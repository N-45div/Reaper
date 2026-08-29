"""Hear the channel the paper trail always misses.

Cancellations get confirmed on the phone. "Yes, that's cancelled, you'll see it
on the next invoice" — and then the next invoice arrives anyway and nobody can
prove the call happened. Every other channel this agent touches leaves a
document; a call leaves a memory, and memory is exactly what a dispute discounts.

So a recording is treated the way this agent treats everything else: read it,
write down what was said, and hash it so the record can be checked later. The
audio's own SHA-256 goes into the receipt beside the transcript, which is what
makes the pair evidence rather than a note - the transcript can be re-derived
from the file, and the file can be shown to be the one that was read.

The model is asked to transcribe and nothing else. It does not summarise, judge
whether the vendor agreed, or decide anything: what was said is a fact, what it
means is for the deterministic checks and the human.
"""

import base64
import hashlib
from pathlib import Path

from dotenv import load_dotenv

from . import llm
from .config import TRANSCRIBE_MODEL

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Transcription only. The moment this prompt starts asking for conclusions, the
# receipt stops being a record of what was said and becomes an opinion.
#
# Tested, not assumed: this is a dedicated speech model, not an instruct model.
# It transcribes and ignores formatting directions - asked for "SPEAKER 1 /
# SPEAKER 2" labels it returns the same unlabelled paragraph either way. So the
# receipt stores an unlabelled verbatim transcript and records the direction of
# the call in who_called, rather than printing labels the model never assigned.
PROMPT = """Transcribe this recording verbatim.

Keep dates, amounts, reference numbers and vendor names exactly as spoken.
Write [INAUDIBLE] for any passage that cannot be made out, rather than guessing
at it. Do not summarise, correct, or complete a sentence the speaker did not
finish."""


def audio_digest(data: bytes) -> str:
    """The recording's own fingerprint, so the transcript can be tied to it."""
    return hashlib.sha256(data).hexdigest()


def transcribe_call(data: bytes, mime_type: str,
                    vocabulary: list[str] | None = None) -> dict:
    """Return {"text", "ok", "model", "sha256", "error"} for a recording.

    Never raises: a call that could not be read is a fact the ledger should
    record, not an exception that loses the recording as well.
    """
    digest = audio_digest(data)
    # The rules travel WITH the audio or they do not apply: without this the
    # model returns an unlabelled paragraph, and "who said you will not be
    # billed" is the one thing the receipt exists to answer.
    prompt = PROMPT
    if vocabulary:
        # Vendor names and contract terms are exactly what generic speech models
        # mangle, and a mangled vendor name is a weaker piece of evidence.
        terms = ", ".join(dict.fromkeys(v.strip() for v in vocabulary if v.strip()))
        prompt += f"\n\nExpect these terms and spell them this way: {terms}."
    # The transcription models answer on the interactions surface, not the
    # generate_content one: asked through generate_content they return an empty
    # part instead of an error - a silence that looks like success.
    content: list[dict] = [{"type": "audio",
                            "data": base64.b64encode(data).decode(),
                            "mime_type": mime_type}]
    if prompt:
        content.append({"type": "text", "text": prompt})
    try:
        resp = llm.call(lambda c: c.interactions.create(
            model=TRANSCRIBE_MODEL, input=content))
        text = (getattr(resp, "output_text", None) or "").strip()
        return {
            "text": text,
            # Silence is a legitimate answer from a transcriber, and it is not
            # the same thing as a failure. Both are recorded; neither is guessed.
            "ok": True,
            "audible": bool(text),
            "model": TRANSCRIBE_MODEL,
            "sha256": digest,
            "inaudible": "[INAUDIBLE]" in text,
            "error": None,
        }
    except Exception as exc:
        return {
            "text": "",
            "ok": False,
            "audible": False,
            "model": TRANSCRIBE_MODEL,
            "sha256": digest,
            "inaudible": False,
            "error": f"{type(exc).__name__}: {exc}"[:200],
        }
