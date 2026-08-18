"""Firestore ledger backend — the evidence chain lives in Google Cloud.

Same public surface and identical hash-chain math as ledger_sqlite. Documents:
  obligations/{id}                obligation fields (id kept as an int field)
  obligations/{id}/receipts/{seq} hash-chained receipts, seq-ordered
  activity/{auto}                 flat mirror of receipts for the live feed
  pending_resume/{id}             resume pointers for paused approvals
  meta/{key}                      clock offset, id counter, misc

Free tier: 1 GiB / 50k reads / 20k writes per day — orders of magnitude above
demo load. Single-writer demo semantics: append uses read-then-write.
"""

import hashlib
import json
from datetime import datetime, timezone

from google.cloud import firestore

from .config import GCP_PROJECT

GENESIS = "0" * 64
_db = None


def _client() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(project=GCP_PROJECT)
    return _db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _next_id() -> int:
    ref = _client().collection("meta").document("obligation_seq")
    tx = _client().transaction()

    @firestore.transactional
    def bump(t):
        snap = ref.get(transaction=t)
        n = (snap.get("value") if snap.exists else 0) + 1
        t.set(ref, {"value": n})
        return n

    return bump(tx)


def create_obligation(**fields) -> int:
    oid = _next_id()
    doc = {**fields, "id": oid, "created_at": _now()}
    _client().collection("obligations").document(str(oid)).set(doc)
    return oid


def get_obligation(obligation_id: int) -> dict | None:
    snap = _client().collection("obligations").document(str(obligation_id)).get()
    return snap.to_dict() if snap.exists else None


def list_obligations() -> list[dict]:
    docs = [d.to_dict() for d in _client().collection("obligations").stream()]
    return sorted(docs, key=lambda d: d["id"])


def set_status(obligation_id: int, status: str) -> None:
    _client().collection("obligations").document(str(obligation_id)).update(
        {"status": status}
    )


def _receipts_ref(obligation_id: int):
    return (
        _client().collection("obligations").document(str(obligation_id))
        .collection("receipts")
    )


def append_receipt(obligation_id: int, kind: str, payload: dict) -> dict:
    ref = _receipts_ref(obligation_id)
    last = list(
        ref.order_by("seq", direction=firestore.Query.DESCENDING).limit(1).stream()
    )
    prev_hash = last[0].get("hash") if last else GENESIS
    seq = (last[0].get("seq") + 1) if last else 1
    ts = _now()
    body = json.dumps(payload, sort_keys=True, default=str)
    digest = hashlib.sha256(f"{prev_hash}|{kind}|{body}|{ts}".encode()).hexdigest()
    ref.document(f"{seq:06d}").set({
        "seq": seq, "kind": kind, "payload": body, "ts": ts,
        "prev_hash": prev_hash, "hash": digest,
    })
    ob = get_obligation(obligation_id)
    _client().collection("activity").add({
        "obligation_id": obligation_id, "vendor": (ob or {}).get("vendor", "?"),
        "kind": kind, "ts": ts, "hash": digest,
    })
    return {"kind": kind, "ts": ts, "hash": digest}


def get_receipts(obligation_id: int) -> list[dict]:
    return [
        d.to_dict()
        for d in _receipts_ref(obligation_id).order_by("seq").stream()
    ]


def verify_chain(obligation_id: int) -> tuple[bool, str | None]:
    prev_hash = GENESIS
    for r in get_receipts(obligation_id):
        if r["prev_hash"] != prev_hash:
            return False, r["hash"]
        expected = hashlib.sha256(
            f"{prev_hash}|{r['kind']}|{r['payload']}|{r['ts']}".encode()
        ).hexdigest()
        if expected != r["hash"]:
            return False, r["hash"]
        prev_hash = r["hash"]
    return True, None


def get_meta(key: str) -> str | None:
    snap = _client().collection("meta").document(key).get()
    return snap.get("value") if snap.exists else None


def set_meta(key: str, value: str) -> None:
    _client().collection("meta").document(key).set({"value": value})


def recent_activity(limit: int = 40) -> list[dict]:
    return [
        d.to_dict()
        for d in _client().collection("activity")
        .order_by("ts", direction=firestore.Query.DESCENDING).limit(limit).stream()
    ]


def save_resume_pointer(obligation_id: int, user_id: str, session_id: str,
                        invocation_id: str, function_call_id: str) -> None:
    _client().collection("pending_resume").document(str(obligation_id)).set({
        "obligation_id": obligation_id, "user_id": user_id,
        "session_id": session_id, "invocation_id": invocation_id,
        "function_call_id": function_call_id, "created_at": _now(),
    })


def pop_resume_pointer(obligation_id: int) -> dict | None:
    ref = _client().collection("pending_resume").document(str(obligation_id))
    snap = ref.get()
    if not snap.exists:
        return None
    ref.delete()
    return snap.to_dict()


def _purge(col_ref) -> None:
    for doc in col_ref.stream():
        for sub in doc.reference.collections():
            _purge(sub)
        doc.reference.delete()


def reset_all() -> None:
    for name in ("obligations", "activity", "pending_resume", "meta"):
        _purge(_client().collection(name))
