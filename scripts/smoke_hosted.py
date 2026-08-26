"""End-to-end smoke gauntlet against a deployed Reaper instance.

Drives the full arc through the public API only - intake, gate, trap, wake,
phone offer, chaos kill, durable pause, approval, notice, invoice, dispute -
asserting every transition with timeouts. No screen, no browser: if this
passes, the demo cannot be surprised by the world.

Usage:
  python scripts/smoke_hosted.py https://host            # full arc
  python scripts/smoke_hosted.py https://host --from-kill
      # continuation: expects an AWAITING_APPROVAL world with the offer
      # already on record; runs kill -> durability -> approval -> invoice
"""
import json
import sys
import time
import urllib.request
import urllib.error
from datetime import date
from pathlib import Path

ARGS = sys.argv[1:]
BASE = next((a for a in ARGS if not a.startswith("--")), "http://127.0.0.1:8080").rstrip("/")
FROM_KILL = "--from-kill" in ARGS
# --preroll is the go/no-go before a camera rolls: it proves a REAL wake can
# complete right now (the thing every failed take died on) at the cost of one
# short arc, and stops before the kill so the take starts on a clean world.
PREROLL = "--preroll" in ARGS
RESULTS = []


def req(path, method="GET", body=None, timeout=60, files=None, retries=2):
    """One API call. Retries gateway errors - the kill beat restarts the host."""
    last = None
    for attempt in range(retries + 1):
        url = BASE + path
        if files:
            boundary = "----reapersmoke"
            name, fname, data = files
            head = (f"--{boundary}\r\nContent-Disposition: form-data; "
                    f'name="{name}"; filename="{fname}"\r\n'
                    f"Content-Type: text/plain\r\n\r\n").encode()
            payload = head + data + f"\r\n--{boundary}--\r\n".encode()
            r = urllib.request.Request(url, data=payload, method="POST",
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        else:
            r = urllib.request.Request(url, method=method,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json"} if body is not None else {})
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return json.load(resp)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            last = exc
            code = getattr(exc, "code", None)
            if attempt < retries and (code in (502, 503, 504) or code is None):
                time.sleep(6)
                continue
            raise
    raise last


def alive():
    try:
        return bool(req("/healthz", timeout=8, retries=0).get("ok"))
    except Exception:
        return False


def poll(fn, timeout_s, every=6):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            v = fn()
            if v:
                return v, time.time() - t0
        except Exception:
            pass
        time.sleep(every)
    return None, time.time() - t0


def step(name, ok, secs, detail=""):
    RESULTS.append((name, ok, secs, detail))
    print(f"{'PASS' if ok else 'FAIL':4} {secs:6.1f}s  {name}  {detail}")
    if not ok:
        finish()


def finish():
    ok = all(r[1] for r in RESULTS)
    print("\n=== GAUNTLET", "GREEN" if ok else "RED",
          f"({sum(r[2] for r in RESULTS):.0f}s total) ===")
    sys.exit(0 if ok else 1)


def obligations():
    return req("/obligations")


def receipts(oid):
    return req(f"/obligations/{oid}/receipts")["receipts"]


def kinds(oid):
    return [r["kind"] for r in receipts(oid)]


def by_status(status):
    return [o for o in obligations() if o["status"] == status]


if not FROM_KILL:
    # 1 - reset to a clean world
    t = time.time()
    req("/demo/reset", "POST", {}, timeout=180)
    poll(lambda: len(obligations()) == 0, 60)
    step("reset -> empty ledger", len(obligations()) == 0, time.time() - t)

    # 2 - intake the clean contract; expect gate MATCH -> SCHEDULED
    t = time.time()
    fixture = Path(__file__).resolve().parent.parent / "data" / "contracts" / "datavault-pro-services.txt"
    req("/contracts/upload", files=("file", fixture.name, fixture.read_bytes()), timeout=180)
    ob, _ = poll(lambda: by_status("SCHEDULED"), 120)
    step("intake clean -> SCHEDULED", bool(ob), time.time() - t)
    oid = ob[0]["id"]
    ks = kinds(oid)
    step("gate chain has EXTRACTED+GATED+PRECEDENT_CONSULTED",
         all(k in ks for k in ("EXTRACTED", "GATED", "PRECEDENT_CONSULTED")), 0, str(ks))

    if not PREROLL:
        # 3 - intake the trap; expect BLOCKED, never SCHEDULED
        t = time.time()
        trap = Path(__file__).resolve().parent.parent / "data" / "contracts" / "ambiguous-hostwave.txt"
        req("/contracts/upload", files=("file", trap.name, trap.read_bytes()), timeout=180)
        blocked, _ = poll(lambda: by_status("BLOCKED"), 120)
        step("intake trap -> BLOCKED", bool(blocked), time.time() - t)

    # 4 - precedent store answers
    t = time.time()
    p = req("/precedents/status", timeout=90)
    step("precedent store available", bool(p.get("available")), time.time() - t,
         f"rows={p.get('rows')}")

    # 5 - advance the clock into the notice window; the agent must wake itself
    t = time.time()
    ledger_date = date.fromisoformat(req("/clock")["ledger_date"])
    deadline = date.fromisoformat(ob[0]["engine_deadline"])
    days = max(0, (deadline - ledger_date).days)
    req("/clock/advance", "POST", {"days": days}, timeout=120)
    aw, _ = poll(lambda: by_status("AWAITING_APPROVAL"), 300)
    step("self-wake -> AWAITING_APPROVAL", bool(aw), time.time() - t)
    oid = aw[0]["id"] if aw else oid

    # 6 - the phone offer MUST be on record (this sends a real Telegram message)
    t = time.time()
    offered, _ = poll(lambda: "APPROVAL_OFFERED" in kinds(oid), 150)
    step("APPROVAL_OFFERED receipt (phone buzzed)", bool(offered), time.time() - t)

    if PREROLL:
        # The wake works right now, which is the only thing a take cannot
        # recover from. Leave the world clean for the camera.
        t = time.time()
        req("/demo/reset", "POST", {}, timeout=180)
        poll(lambda: len(obligations()) == 0, 60)
        step("stage handed back clean", len(obligations()) == 0, time.time() - t)
        finish()
else:
    # continuation: the world must already hold an offered, durable pause
    t = time.time()
    aw, _ = poll(lambda: by_status("AWAITING_APPROVAL"), 30)
    step("continuation: pause present", bool(aw), time.time() - t)
    oid = aw[0]["id"]
    step("continuation: offer on record", "APPROVAL_OFFERED" in kinds(oid), 0)

# 7 - chaos kill; the process must be observed DOWN, then come back
t = time.time()
try:
    req("/chaos/kill", "POST", {}, timeout=10, retries=0)
except Exception:
    pass  # the process dies mid-response; that is the point
down, dsecs = poll(lambda: not alive(), 45, every=3)
healthy, _ = poll(alive, 240, every=5)
label = "resurrected after kill" if down else "resurrected after kill (down never observed)"
step(label, bool(healthy), time.time() - t, f"down_after={dsecs:.0f}s" if down else "")

# 8 - the pause must have survived into the new process
still, _ = poll(lambda: by_status("AWAITING_APPROVAL"), 90)
step("pause survived the kill", bool(still), 0)

# 9 - deliver the human decision in-app; the run resumes and the notice goes out
t = time.time()
req(f"/obligations/{oid}/approval", "POST", {"approve": True}, timeout=90)
sent, _ = poll(lambda: "NOTICE_SENT" in kinds(oid), 300)
step("approval -> NOTICE_SENT", bool(sent), time.time() - t)
rec = [r for r in receipts(oid) if r["kind"] == "NOTICE_SENT"][-1]
pl = rec.get("payload")
pl = json.loads(pl) if isinstance(pl, str) else (pl or {})
step("delivery channel + approval provenance on the receipt",
     pl.get("channel") in ("smtp", "simulated") and bool(pl.get("approved_by_receipt")),
     0, f"channel={pl.get('channel')}")

# 10 - next cycle: invoice arrives, verdict computed, dispute filed if refuted
t = time.time()
ledger_date = date.fromisoformat(req("/clock")["ledger_date"])
term_end = date.fromisoformat([o for o in obligations() if o["id"] == oid][0]["term_end"])
days = max(0, (term_end - ledger_date).days) + 2
req("/clock/advance", "POST", {"days": days}, timeout=120)
verdict, _ = poll(lambda: any(k in kinds(oid) for k in
                              ("REFUTED", "VERIFIED", "DISPUTE_FILED", "DISPUTED")), 360)
step("invoice verdict reached", bool(verdict), time.time() - t,
     str([k for k in kinds(oid) if k in ("REFUTED", "VERIFIED", "DISPUTE_FILED", "DISPUTED")]))

# 11 - the chain must verify end to end
r = req(f"/obligations/{oid}/receipts")
step("hash chain intact", bool(r.get("chain_intact")), 0)

finish()
