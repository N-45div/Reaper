"""Shared Gemini access with quota resilience.

Gemini API quotas bite hardest exactly when nobody is watching, and this agent
does its most important work unattended — waking at night, reading an invoice
weeks after a notice went out. A throttled call must never look like a verdict.
So every direct model call goes through here: transient 429s are retried with
backoff, and if several API keys are configured the client rotates to the next
one rather than giving up.

Nothing here decides anything. If every attempt fails the caller is told
plainly, and the ledger records that the document could not be read.
"""

import os
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from .config import MODEL_LADDER

_RETRYABLE = ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "INTERNAL")
_index = 0
_model_index = 0


def keys() -> list[str]:
    """Every configured key, primary first."""
    found, seen = [], set()
    for raw in (os.getenv("GOOGLE_API_KEY", ""), os.getenv("GOOGLE_API_KEYS", "")):
        for k in raw.split(","):
            k = k.strip()
            if k and k not in seen:
                seen.add(k)
                found.append(k)
    return found


_clients: dict[str, genai.Client] = {}


def client() -> genai.Client:
    """The client for the currently selected key.

    Clients are memoised per key: each one owns a connection pool, so handing
    out a fresh client per call would tear sessions down underneath in-flight
    requests. Rotation swaps between long-lived clients instead.
    """
    ks = keys()
    if not ks:
        raise RuntimeError("no GOOGLE_API_KEY configured")
    key = ks[_index % len(ks)]
    if key not in _clients:
        _clients[key] = genai.Client(api_key=key)
    return _clients[key]


def _retryable(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return any(token in text for token in _RETRYABLE)


def call(fn, *, attempts: int = 4, base_delay: float = 4.0):
    """Run fn(client) with rotation and backoff. Raises the last error."""
    global _index
    ks = keys() or [""]
    last = None
    for attempt in range(attempts):
        try:
            return fn(client())
        except Exception as exc:
            last = exc
            if not _retryable(exc) or attempt == attempts - 1:
                raise
            _index += 1  # try the next key before waiting on this one
            if _index % len(ks) == 0:
                time.sleep(base_delay * (2 ** attempt))
    raise last


def current_model() -> str:
    """The model the agent should be using right now."""
    return MODEL_LADDER[_model_index % len(MODEL_LADDER)]


def rotate() -> None:
    """Step to the next key; once every key is spent, step down the ladder.

    API quota is granted per project and per model, so a run that dies on
    one combination usually succeeds on another. Nothing about the agent's
    behaviour changes — only which endpoint answers it.
    """
    global _index, _model_index
    _index += 1
    if len(keys()) and _index % len(keys()) == 0:
        _model_index += 1


def is_quota_error(exc: Exception) -> bool:
    return _retryable(exc)
