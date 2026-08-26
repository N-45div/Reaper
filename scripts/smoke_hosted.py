"""End-to-end smoke gauntlet against a deployed Reaper instance.

Drives the full arc through the public API only - intake, gate, trap, wake,
phone offer, chaos kill, durable pause, approval, notice, invoice, dispute -
asserting every transition with timeouts. No screen, no browser: if this
passes, the demo cannot be surprised by the world.

Usage: python scripts/smoke_hosted.py https://reaper-sxxs.onrender.com
"""
import json
import sys
import time
import urllib.request
import urllib.error
from datetime import date
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080").rstrip("/")
RESULTS = []


def req(path, method="GET", body=None, timeout=60, files=None):
    url = BASE + path
    if files:
        boundary = "----reapersmoke"
        name, fname, data = files
        payload = (f"--{boundary}\r\nContent-Disposition: form-data; "
                   f'name="{name}"; filename="{fname}"\r\n'
                   f"Content-Type: text/plain\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        r = urllib.request.Request(url, data=payload, method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        r = urllib.request.Request(url, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"} if body is not None else {})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.load(resp)


def poll(desc, fn, timeout_s, every=6):
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
    print("\n=== GAUNTLET", "GREEN" if ok else "RED", f"({sum(r[2] for r in RESULTS):.0f}s total) ===")
    sys.exit(0 if ok else 1)


def obligations():
    return req("/obligations")


def receipts(oid):
    return req(f"/obligations/{oid}/receipts")["receipts"]


def kinds(oid):
    return [r["kind"] for r in receipts(oid)]


def by_status(status):
    return [o for o in obligations() if o["status"] == status]


t0 = time.time()

# 1 - reset to a clean world
req("/demo/reset", "POST", {}, timeout=180)
_, secs = poll("empty", lambda: len(obligations()) == 0, 60)
step("reset -> empty ledger", len(obligations()) == 0, time.time() - t0)

# 2 - intake the clean contract; expect gate MATCH -> SCHEDULED
t = time.time()
fixture = Path(__file__).resolve().parent.parent / "data" / "contracts" / "datavault-pro-services.txt"
req("/contracts/upload", files=("file", fixture.name, fixture.read_bytes()), timeout=180)
ob, _ = poll("sched", lambda: by_status("SCHEDULED"), 120)
step("intake clean -> SCHEDULED", bool(ob), time.time() - t)
oid = ob[0]["id"]
ks = kinds(oid)
step("gate chain has EXTRACTED+GATED+PRECEDENT_CONSULTED",
     all(k in ks for k in ("EXTRACTED", "GATED", "PRECEDENT_CONSULTED")), 0, str(ks))

# 3 - intake the trap; expect BLOCKED, never SCHEDULED
t = time.time()
trap = Path(__file__).resolve().parent.parent / "data" / "contracts" / "ambiguous-hostwave.txt"
req("/contracts/upload", files=("file", trap.name, trap.read_bytes()), timeout=180)
blocked, _ = poll("blocked", lambda: by_status("BLOCKED"), 120)
step("intake trap -> BLOCKED", bool(blocked), time.time() - t)

# 4 - precedent store answers
t = time.time()
p = req("/precedents/status", timeout=90)
step("precedent store available", bool(p.get("available")), time.time() - t, f"rows={p.get('rows')}")

# 5 - advance the clock into the notice window; the agent must wake itself
t = time.time()
ledger_date = date.fromisoformat(req("/clock")["ledger_date"])
deadline = date.fromisoformat(ob[0]["engine_deadline"])
days = max(0, (deadline - ledger_date).days)
req("/clock/advance", "POST", {"days": days}, timeout=120)
aw, secs = poll("awaiting", lambda: by_status("AWAITING_APPROVAL"), 300)
step("self-wake -> AWAITING_APPROVAL", bool(aw), time.time() - t)

# 6 - the phone offer MUST be on record (this sends a real Telegram message)
t = time.time()
offered, secs = poll("offered", lambda: "APPROVAL_OFFERED" in kinds(oid), 90)
step("APPROVAL_OFFERED receipt (phone buzzed)", bool(offered), time.time() - t)

# 7 - chaos kill; the pause must survive the new process
t = time.time()
try:
    req("/chaos/kill", "POST", {}, timeout=10)
except Exception:
    pass  # the process dies mid-response; that is the point
healthy, secs = poll("health", lambda: req("/healthz", timeout=8).get("ok"), 180, every=5)
step("resurrected after kill", bool(healthy), time.time() - t)
still = by_status("AWAITING_APPROVAL")
step("pause survived the kill", bool(still), 0)

# 8 - deliver the human decision in-app; the run resumes and the notice goes out
t = time.time()
req(f"/obligations/{oid}/approval", "POST", {"approve": True}, timeout=90)
sent, secs = poll("sent", lambda: "NOTICE_SENT" in kinds(oid), 300)
step("approval -> NOTICE_SENT", bool(sent), time.time() - t)
if sent:
    rec = [r for r in receipts(oid) if r["kind"] == "NOTICE_SENT"][-1]
    ch = json.loads(rec["payload"]).get("channel") if isinstance(rec.get("payload"), str) else rec.get("payload", {}).get("channel")
    step("delivery channel named on the receipt", ch in ("smtp", "simulated"), 0, f"channel={ch}")

# 9 - next cycle: invoice arrives, verdict computed, dispute filed if refuted
t = time.time()
ledger_date = date.fromisoformat(req("/clock")["ledger_date"])
term_end = date.fromisoformat(obligations()[0]["term_end"])
days = max(0, (term_end - ledger_date).days) + 2
req("/clock/advance", "POST", {"days": days}, timeout=120)
verdict, secs = poll("verdict", lambda: any(k in kinds(oid) for k in ("REFUTED", "VERIFIED", "DISPUTE_FILED", "DISPUTED")), 360)
step("invoice verdict reached", bool(verdict), time.time() - t, str([k for k in kinds(oid) if k in ("REFUTED","VERIFIED","DISPUTE_FILED","DISPUTED")]))

# 10 - the chain must verify end to end
t = time.time()
r = req(f"/obligations/{oid}/receipts")
step("hash chain intact", bool(r.get("chain_intact")), time.time() - t)

finish()
