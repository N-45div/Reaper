"""The briefing: a board-ready deck the agent writes from its own ledger.

Gemini supplies the judgement — what is urgent, what is exposed, what to do —
while every number on the slides (days remaining, counts, chain integrity) is
computed here in plain code. Same division of labour as the date gate: the
model narrates, the machine counts.
"""

import json
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from google.genai import types

from . import clock, ledger, llm
from .config import MODEL

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "standfirst": {"type": "string"},
        "situation": {"type": "string"},
        "obligations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "verdict": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["id", "verdict", "note"],
            },
        },
        "risks": {"type": "array", "items": {"type": "string"}},
        "recommendation": {"type": "string"},
    },
    "required": ["headline", "standfirst", "situation", "obligations", "risks", "recommendation"],
}

PROMPT = """You are Reaper, an autonomous contract-renewal agent, writing the
standing briefing for the finance director who owns these vendor relationships.

Write like a good analyst: specific, unhurried, no marketing language, no
exclamation marks, no invented facts. You may only use the data given below.

- headline: six words or fewer, the state of the portfolio.
- standfirst: one sentence a director could read and stop there.
- situation: two or three sentences on where the portfolio stands overall.
- obligations: one entry per obligation. verdict is at most four words
  ("on track", "needs a signature", "blocked pending review", "in dispute").
  note is one sentence on what it means for the business and what happens next.
- risks: two to four concrete risks, each one sentence. If an obligation is
  BLOCKED, say plainly that no deadline could be verified and a human must read
  the clause.
- recommendation: two sentences on what the director should do this week.

IMPORTANT — do not infer history from dates. Each obligation carries explicit
facts. A notice_deadline in the past does NOT mean the deadline was missed:
check notice_served and notice_served_on. If notice_served is true, the notice
was delivered on time by this agent. If billing_stopped is false, the vendor
charged anyway after valid notice — that is the vendor's failure, not ours, and
dispute_opened tells you whether the agent has already filed against it.

PORTFOLIO DATA
%s
"""


def _days(target: str | None, today: date) -> int | None:
    if not target:
        return None
    return (date.fromisoformat(target) - today).days


def _history(obligation_id: int) -> dict:
    """What actually happened, read off the receipts.

    The narrator is given these facts explicitly rather than being left to infer
    them from dates: a notice deadline in the past means nothing on its own, and
    a briefing that guesses wrong about whether notice was served is worse than
    no briefing at all.
    """
    facts = {
        "notice_served": False, "notice_served_on": None,
        "invoice_checked": False, "amount_billed": None, "amount_expected": None,
        "billing_stopped": None, "dispute_opened": False,
        "gate_blocked_because": None, "read_from": None,
    }
    for r in ledger.get_receipts(obligation_id):
        try:
            payload = json.loads(r["payload"])
        except (ValueError, TypeError):
            payload = {}
        kind = r["kind"]
        if kind == "NOTICE_SENT":
            facts["notice_served"] = True
            facts["notice_served_on"] = payload.get("delivered_on") or r["ts"][:10]
        elif kind == "GATED" and payload.get("verdict") in ("MISMATCH", "AMBIGUOUS"):
            reasons = payload.get("reasons") or []
            facts["gate_blocked_because"] = reasons[0] if reasons else payload.get("verdict")
        elif kind == "INVOICE_CHECKED":
            facts["invoice_checked"] = True
            facts["amount_billed"] = payload.get("billed")
            facts["read_from"] = payload.get("read_by")
        elif kind in ("VERIFIED", "REFUTED"):
            facts["amount_billed"] = payload.get("billed", facts["amount_billed"])
            facts["amount_expected"] = payload.get("expected")
            facts["billing_stopped"] = kind == "VERIFIED"
        elif kind == "DISPUTE_OPENED":
            facts["dispute_opened"] = True
    return facts


def gather() -> dict:
    """Deterministic portfolio facts — every figure the deck shows."""
    today = clock.today()
    rows = []
    for ob in ledger.list_obligations():
        intact, _ = ledger.verify_chain(ob["id"])
        rows.append({
            "id": ob["id"],
            "vendor": ob["vendor"],
            "status": ob["status"],
            "gate_verdict": ob["gate_verdict"],
            "clause": (ob["clause_text"] or "")[:400],
            "term_end": ob["term_end"],
            "notice_deadline": ob["engine_deadline"],
            "days_to_notice": _days(ob["engine_deadline"], today),
            "days_to_term_end": _days(ob["term_end"], today),
            "receipts": len(ledger.get_receipts(ob["id"])),
            "chain_intact": intact,
            **_history(ob["id"]),
        })
    rows.sort(key=lambda r: (r["days_to_notice"] is None, r["days_to_notice"]))
    return {
        "as_of": today.isoformat(),
        "count": len(rows),
        "blocked": sum(1 for r in rows if r["status"] == "BLOCKED"),
        "awaiting": sum(1 for r in rows if r["status"] == "AWAITING_APPROVAL"),
        "disputed": sum(1 for r in rows if r["status"] == "DISPUTED"),
        "verified": sum(1 for r in rows if r["status"] == "VERIFIED"),
        "all_chains_intact": all(r["chain_intact"] for r in rows) if rows else True,
        "obligations": rows,
    }


def narrate(facts: dict) -> dict:
    try:
        resp = llm.call(lambda c: c.models.generate_content(
            model=MODEL,
            contents=PROMPT % json.dumps(facts, indent=2),
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=SCHEMA),
        ))
        return json.loads(resp.text)
    except Exception as exc:
        return {
            "headline": "Portfolio briefing",
            "standfirst": f"{facts['count']} obligations under watch as of {facts['as_of']}.",
            "situation": "The narrative model was unavailable, so this briefing shows "
                         "the ledger figures without commentary. Every number below is "
                         "computed from the evidence chain, not written by a model.",
            "obligations": [{"id": r["id"], "verdict": r["status"].replace("_", " ").lower(),
                             "note": ""} for r in facts["obligations"]],
            "risks": [], "recommendation": "",
            "degraded": f"{type(exc).__name__}",
        }
