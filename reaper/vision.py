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
from google import genai
from google.genai import types

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
        client = genai.Client(api_key=os.environ["GOOGLE_API_KEY"])
        resp = client.models.generate_content(
            model=MODEL,
            contents=[
                types.Part.from_bytes(data=data, mime_type=mime_type),
                types.Part(text=PROMPT),
            ],
        )
        text = (resp.text or "").strip()
        return {"text": text, "ok": bool(text), "model": MODEL,
                "illegible": "[ILLEGIBLE]" in text}
    except Exception as exc:
        return {"text": "", "ok": False, "model": MODEL,
                "error": f"{type(exc).__name__}: {exc}"[:200]}
