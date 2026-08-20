"""Multimodal intake: read a contract the agent was never handed as text.

A photograph of a paper agreement, a scan, a screenshot of a vendor portal —
Gemini reads the pixels and returns the contract text, which then flows through
exactly the same pipeline as an uploaded file: Gemma triage, clause extraction,
and the deterministic date gate. The gate is what makes this safe to do at all:
OCR of a curled page can misread "60" as "80", and the engine's independent
re-derivation is what catches it.
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from google.genai import types

from . import llm
from .config import MODEL

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PROMPT = """Transcribe this contract image to plain text.

Rules:
- Reproduce the wording EXACTLY. Never paraphrase, correct, or complete text.
- Pay particular attention to renewal and termination clauses: transcribe every
  number and every written-out numeral exactly as printed, including
  constructions like "sixty (60) days".
- Preserve section numbers and headings. Skip page furniture (page numbers,
  headers, footers, watermarks).
- If a passage is genuinely illegible, write [ILLEGIBLE] rather than guessing.

Output the transcription only."""


def transcribe_contract_image(data: bytes, mime_type: str) -> dict:
    """Return {"text": ..., "ok": bool, "model": ...} for an image or PDF."""
    try:
        resp = llm.call(lambda c: c.models.generate_content(
            model=MODEL,
            contents=[
                types.Part.from_bytes(data=data, mime_type=mime_type),
                types.Part(text=PROMPT),
            ],
        ))
        text = (resp.text or "").strip()
        return {"text": text, "ok": bool(text), "model": MODEL,
                "illegible": "[ILLEGIBLE]" in text}
    except Exception as exc:
        return {"text": "", "ok": False, "model": MODEL,
                "error": f"{type(exc).__name__}: {exc}"[:200]}


INVOICE_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor": {"type": "string"},
        "invoice_number": {"type": "string"},
        "invoice_date": {"type": "string", "description": "ISO YYYY-MM-DD if legible"},
        "currency": {"type": "string", "description": "ISO code, e.g. USD, INR, GBP"},
        "total_due": {"type": "number", "description": "the final amount payable"},
        "description": {"type": "string", "description": "the line-item description"},
        "legible": {"type": "boolean", "description": "false if the total could not be read with confidence"},
    },
    "required": ["vendor", "currency", "total_due", "description", "legible"],
}

INVOICE_PROMPT = """Read this vendor invoice and report exactly what it says.

Report the TOTAL DUE — the final amount payable, after any subtotal and tax
lines. Do not calculate, infer or correct anything: if the document shows a
total, report that figure. If the total is not legible, set legible to false
and total_due to 0.

Never guess a vendor name or an amount that is not printed on the document."""


def read_invoice(data: bytes, mime_type: str = "image/jpeg") -> dict:
    """Extract the billed amount from an invoice document.

    The model reads the figure; it does not decide anything. Whether the vendor
    honoured the cancellation is settled downstream by comparing this number
    with the amount recorded when the obligation was scheduled.
    """
    try:
        resp = llm.call(lambda c: c.models.generate_content(
            model=MODEL,
            contents=[
                types.Part.from_bytes(data=data, mime_type=mime_type),
                types.Part(text=INVOICE_PROMPT),
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=INVOICE_SCHEMA),
        ))
        import json
        seen = json.loads(resp.text)
        seen["read_by"] = f"{MODEL} (vision)"
        seen["ok"] = True
        return seen
    except Exception as exc:
        return {"ok": False, "legible": False, "read_by": "unavailable",
                "error": f"{type(exc).__name__}: {exc}"[:200]}
