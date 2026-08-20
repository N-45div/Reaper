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
from google import genai
from google.genai import types

from . import clock, ledger
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

PORTFOLIO DATA
%s
"""


def _days(target: str | None, today: date) -> int | None:
    if not target:
        return None
    return (date.fromisoformat(target) - today).days


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
        client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        resp = client.models.generate_content(
            model=MODEL,
            contents=PROMPT % json.dumps(facts, indent=2),
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=SCHEMA),
        )
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
