"""Gemma-powered intake triage.

A lightweight open-model filter in front of the Gemini agent: does this
document even contain an auto-renewal clause? Saves the expensive agent run
on irrelevant uploads. Fails OPEN — triage is an optimization, never a gate:
if Gemma is unavailable the document proceeds to full intake.
"""

import json

from google import genai

from .config import TRIAGE_MODEL

_PROMPT = """You are a fast contract triage filter. Answer strictly with JSON:
{"has_renewal": true|false, "section_hint": "<section number or heading of the auto-renewal clause, or empty>"}

Does the following document contain an automatic-renewal clause (a term that
renews the agreement unless notice is given)?

--- DOCUMENT ---
"""


def triage_contract(text: str) -> dict:
    try:
        client = genai.Client()
        resp = client.models.generate_content(
            model=TRIAGE_MODEL,
            contents=_PROMPT + text[:8000],
        )
        raw = resp.text.strip()
        raw = raw[raw.find("{"): raw.rfind("}") + 1]
        data = json.loads(raw)
        return {
            "has_renewal": bool(data.get("has_renewal", True)),
            "section_hint": str(data.get("section_hint", ""))[:200],
            "model": TRIAGE_MODEL,
            "ok": True,
        }
    except Exception as exc:
        return {"has_renewal": True, "section_hint": "", "model": TRIAGE_MODEL,
                "ok": False, "error": type(exc).__name__}
