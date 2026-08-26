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
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from .config import MODEL_LADDER

_RETRYABLE = ("429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "INTERNAL")
_index = 0
_model_index = 0

# Free-tier Gemini quota is granted per PROJECT and per MODEL:
# "GenerateRequestsPerDayPerProjectPerModel-FreeTier", 20 requests a day.
# So every (api key, model) pair is its own small daily bucket, and the way to
# survive a spent bucket is to move to a different pair - not to wait, and not
# to keep asking the one that just said no. A bucket that answers 429 is
# remembered as spent so no later call wastes an attempt rediscovering it.
_DRY_SECONDS = float(os.getenv("REAPER_DRY_SECONDS", "120"))
_dry: dict[tuple[int, str], float] = {}

# Retrying a daily-quota refusal is worse than useless: the allowance will not
# come back within the retry window, and every attempt spends another request
# from a bucket that has none. So the transport retries genuinely transient
# server errors and NEVER a 429 - that case belongs to bucket rotation.
_HTTP_OPTIONS = genai_types.HttpOptions(
    retry_options=genai_types.HttpRetryOptions(
        attempts=3,
        http_status_codes=[500, 502, 503, 504],
    )
)


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
        _clients[key] = genai.Client(api_key=key, http_options=_HTTP_OPTIONS)
    return _clients[key]


def _describe(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _is_call_budget(text: str) -> bool:
    """The agent framework's own per-turn call cap, which is not a quota error.

    Its message contains the number 500, and a substring match once read that
    as a server error - so a capped turn was retried as if the API had failed,
    and the bucket it was using was blamed and locked out.
    """
    low = text.lower()
    return "llmcallslimit" in low or "llm calls limit" in low


def _retryable(exc: Exception) -> bool:
    text = _describe(exc)
    if _is_call_budget(text):
        return False
    return any(token in text for token in _RETRYABLE)


def retry_after(exc: Exception) -> float | None:
    """How long the API itself asked us to wait, in seconds, if it said."""
    m = re.search(r"'retryDelay': '(\d+(?:\.\d+)?)s'", f"{exc}")
    if m:
        return float(m.group(1))
    m = re.search(r"[Pp]lease retry in (\d+(?:\.\d+)?)s", f"{exc}")
    return float(m.group(1)) if m else None


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
            if is_quota_error(exc):
                mark_dry(exc)     # this pair is spent; do not come back to it
                rotate()          # straight to a pair that still has quota
            else:
                _advance()        # transient: the next key may simply be luckier
                if _index % len(ks) == 0:
                    time.sleep(base_delay * (2 ** attempt))
    raise last


def current_model() -> str:
    """The model the agent should be using right now."""
    return MODEL_LADDER[_model_index % len(MODEL_LADDER)]


def current_bucket() -> tuple[int, str]:
    """The (key index, model) pair that would serve the next call."""
    ks = keys() or [""]
    return (_index % len(ks), current_model())


def _is_dry(bucket: tuple[int, str]) -> bool:
    until = _dry.get(bucket)
    return until is not None and time.monotonic() < until


def mark_dry(exc: Exception | None = None, seconds: float | None = None) -> None:
    """Remember that the current (key, model) bucket has no quota left.

    The daily limit is small and the API says so plainly; recording the refusal
    means the next call skips this pair instead of spending an attempt on it.
    """
    bucket = current_bucket()
    wait = seconds
    if exc is not None:
        text = f"{exc}"
        for candidate in MODEL_LADDER:                 # the error names the model
            if candidate in text:
                bucket = (bucket[0], candidate)
                break
        if wait is None:
            # The refusal carries its own recovery time. Trust it over a guess:
            # sitting out ninety minutes on a request that would be served
            # again in one is quota thrown away, not quota saved.
            told = retry_after(exc)
            if told is not None:
                wait = told + 15
    _dry[bucket] = time.monotonic() + (wait if wait is not None else _DRY_SECONDS)


def _advance() -> None:
    global _index, _model_index
    ks = keys() or [""]
    _index += 1
    if _index % len(ks) == 0:
        _model_index += 1


def buckets_available() -> int:
    """How many (key, model) pairs are not known to be spent."""
    ks = keys() or [""]
    return sum(1 for i in range(len(ks)) for m in MODEL_LADDER
               if not _is_dry((i, m)))


def bucket_report() -> dict:
    """What quota is believed to remain, for diagnostics and pre-flight checks."""
    ks = keys() or [""]
    now = time.monotonic()
    return {
        "keys": len(ks),
        "models": list(MODEL_LADDER),
        "available": buckets_available(),
        "total": len(ks) * len(MODEL_LADDER),
        "spent": [{"key": i, "model": m, "free_in_s": round(_dry[(i, m)] - now)}
                  for (i, m) in sorted(_dry, key=lambda b: (b[0], b[1]))
                  if _dry[(i, m)] > now],
        "current": {"key": current_bucket()[0], "model": current_bucket()[1]},
    }


def rotate() -> None:
    """Move to the next (key, model) bucket that still has quota.

    Free-tier quota is per project AND per model, so a call that dies on one
    pair usually succeeds on another. Pairs already known to be spent are
    stepped over rather than retried. Nothing about the agent's behaviour
    changes — only which endpoint answers it.
    """
    ks = keys() or [""]
    for _ in range(len(ks) * len(MODEL_LADDER)):
        _advance()
        if not _is_dry(current_bucket()):
            return
    _advance()  # every pair is spent; keep moving rather than hammering one


def is_transient(exc: Exception) -> bool:
    """A failure that says nothing about us and everything about right now.

    An overloaded model answers 503; a busy backend answers 500. Neither means
    our allowance is gone, so the bucket must not be blamed - but neither
    should the turn die, because another model is very likely free.
    """
    text = _describe(exc)
    if _is_call_budget(text):
        return False
    return any(tok in text for tok in
               ("503", "UNAVAILABLE", "500", "INTERNAL", "ServerError",
                "overloaded", "high demand", "DEADLINE_EXCEEDED", "TimeoutError"))


def is_quota_error(exc: Exception) -> bool:
    """True only for an actual quota refusal - not every transient failure.

    This decides whether a (key, model) bucket gets blamed and stepped over,
    so a 503 or a capped turn must never qualify: blaming a healthy bucket
    takes capacity away that was never spent.
    """
    text = _describe(exc)
    if _is_call_budget(text):
        return False
    return "429" in text or "RESOURCE_EXHAUSTED" in text
