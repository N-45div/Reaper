"""Ask every (API key, model) pair whether it still has quota today.

Free-tier Gemini grants "GenerateRequestsPerDayPerProjectPerModel-FreeTier" -
a small daily allowance per project AND per model. With several keys from
several projects, capacity is a matrix, not a number, and the only honest way
to know what is left is to ask. Each probe is one realistic request: the same
tool declarations the agent uses, so a pair that answers here can serve a
real turn.

Usage: python scripts/quota_census.py [--models a,b] [--json]
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import dotenv_values

ENV = dotenv_values(Path(__file__).resolve().parent.parent / ".env")

KEYS = []
_seen = set()
for _raw in (ENV.get("GOOGLE_API_KEY", ""), ENV.get("GOOGLE_API_KEYS", "")):
    for _k in (_raw or "").split(","):
        _k = _k.strip()
        if _k and _k not in _seen:
            _seen.add(_k)
            KEYS.append(_k)

DEFAULT_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash",
                  "gemini-3.5-flash-lite", "gemini-3.7-flash"]
MODELS = DEFAULT_MODELS
for i, a in enumerate(sys.argv):
    if a == "--models" and i + 1 < len(sys.argv):
        MODELS = [m.strip() for m in sys.argv[i + 1].split(",") if m.strip()]
AS_JSON = "--json" in sys.argv

TOOLS = [{"functionDeclarations": [{
    "name": "request_notice_approval",
    "description": "Ask the human to approve sending the cancellation notice.",
    "parameters": {"type": "OBJECT", "properties": {
        "obligation_id": {"type": "INTEGER"},
        "notice_summary": {"type": "STRING"}}}}]}]

BODY = {
    "contents": [{"role": "user", "parts": [{"text":
        "NOTICE. The notice window is open for obligation 1 (vendor DataVault "
        "Pro, deadline 2026-11-30). Draft the notice and request approval."}]}],
    "tools": TOOLS,
    "generationConfig": {"maxOutputTokens": 64},
}


def probe(key: str, model: str) -> dict:
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}"
        f":generateContent?key={key}",
        data=json.dumps(BODY).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            json.load(r)
        return {"ok": True, "state": "OK"}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        if exc.code == 429:
            quota_id, limit = "?", "?"
            try:
                for d in json.loads(raw)["error"].get("details", []):
                    for v in d.get("violations", []):
                        quota_id = v.get("quotaId", quota_id)
                        limit = v.get("quotaValue", limit)
            except Exception:
                pass
            return {"ok": False, "state": "SPENT", "quota_id": quota_id,
                    "limit": limit}
        return {"ok": False, "state": f"HTTP {exc.code}",
                "detail": raw[:120]}
    except Exception as exc:
        return {"ok": False, "state": type(exc).__name__}


def main() -> int:
    if not KEYS:
        print("no API keys configured")
        return 2
    rows, usable = [], 0
    for ki, key in enumerate(KEYS):
        for model in MODELS:
            result = probe(key, model)
            rows.append({"key": ki, "key_tail": key[-6:], "model": model, **result})
            if result["ok"]:
                usable += 1
            if not AS_JSON:
                extra = ""
                if result.get("quota_id"):
                    extra = f"  [{result['quota_id']} limit={result.get('limit')}]"
                elif result.get("detail"):
                    extra = f"  {result['detail']}"
                print(f"key{ki} ...{key[-6:]:>6}  {model:<24} {result['state']}{extra}")
            time.sleep(2)
    if AS_JSON:
        print(json.dumps({"usable": usable, "total": len(rows), "rows": rows},
                         indent=2))
    else:
        print(f"\nUSABLE {usable}/{len(rows)} buckets")
        by_model = {}
        for r in rows:
            by_model.setdefault(r["model"], []).append(r["ok"])
        for model, oks in by_model.items():
            print(f"  {model:<24} {sum(oks)}/{len(oks)} keys have capacity")
    return 0 if usable else 1


if __name__ == "__main__":
    raise SystemExit(main())
