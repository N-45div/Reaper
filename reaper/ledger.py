"""Obligation ledger with a hash-chained receipt trail.

Separate sqlite file from ADK's session DB: the ledger is the product's
evidence artifact, the session DB is runtime plumbing. Every state change
appends a receipt whose hash covers the previous receipt's hash — tampering
with any historical entry breaks the chain from that point forward.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA_DIR

LEDGER_PATH = DATA_DIR / "ledger.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS obligations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor TEXT NOT NULL,
    contract_file TEXT,
    clause_text TEXT NOT NULL,
    term_end TEXT NOT NULL,
    llm_deadline TEXT,
    engine_deadline TEXT,
    gate_verdict TEXT NOT NULL,
    status TEXT NOT NULL,
    notice_method TEXT,
    recipient TEXT,
    expected_final_amount REAL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    obligation_id INTEGER NOT NULL REFERENCES obligations(id),
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    ts TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_resume (
    obligation_id INTEGER PRIMARY KEY REFERENCES obligations(id),
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    function_call_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

GENESIS = "0" * 64


def _connect() -> sqlite3.Connection:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LEDGER_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_obligation(**fields) -> int:
    with _connect() as conn:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = conn.execute(
            f"INSERT INTO obligations ({cols}, created_at) VALUES ({marks}, ?)",
            [*fields.values(), _now()],
        )
        return cur.lastrowid


def get_obligation(obligation_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM obligations WHERE id = ?", (obligation_id,)
        ).fetchone()
        return dict(row) if row else None


def list_obligations() -> list[dict]:
    with _connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM obligations ORDER BY id")]


def set_status(obligation_id: int, status: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE obligations SET status = ? WHERE id = ?", (status, obligation_id)
        )


def append_receipt(obligation_id: int, kind: str, payload: dict) -> dict:
    with _connect() as conn:
        prev = conn.execute(
            "SELECT hash FROM receipts WHERE obligation_id = ? ORDER BY id DESC LIMIT 1",
            (obligation_id,),
        ).fetchone()
        prev_hash = prev["hash"] if prev else GENESIS
        ts = _now()
        body = json.dumps(payload, sort_keys=True, default=str)
        digest = hashlib.sha256(f"{prev_hash}|{kind}|{body}|{ts}".encode()).hexdigest()
        conn.execute(
            "INSERT INTO receipts (obligation_id, kind, payload, ts, prev_hash, hash) VALUES (?,?,?,?,?,?)",
            (obligation_id, kind, body, ts, prev_hash, digest),
        )
        return {"kind": kind, "ts": ts, "hash": digest}


def get_receipts(obligation_id: int) -> list[dict]:
    with _connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM receipts WHERE obligation_id = ? ORDER BY id",
                (obligation_id,),
            )
        ]


def verify_chain(obligation_id: int) -> tuple[bool, str | None]:
    """Recompute the whole chain; returns (intact, first_broken_hash)."""
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


def recent_activity(limit: int = 40) -> list[dict]:
    """All receipts across obligations, newest first, with vendor names."""
    with _connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT r.id, r.obligation_id, r.kind, r.ts, r.hash, o.vendor "
                "FROM receipts r JOIN obligations o ON o.id = r.obligation_id "
                "ORDER BY r.id DESC LIMIT ?",
                (limit,),
            )
        ]


def reset_all() -> None:
    """Demo reset: wipe all obligations, receipts and resume pointers."""
    with _connect() as conn:
        conn.execute("DELETE FROM receipts")
        conn.execute("DELETE FROM pending_resume")
        conn.execute("DELETE FROM obligations")


def save_resume_pointer(obligation_id: int, user_id: str, session_id: str,
                        invocation_id: str, function_call_id: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pending_resume VALUES (?,?,?,?,?,?)",
            (obligation_id, user_id, session_id, invocation_id, function_call_id, _now()),
        )


def pop_resume_pointer(obligation_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM pending_resume WHERE obligation_id = ?", (obligation_id,)
        ).fetchone()
        if row:
            conn.execute(
                "DELETE FROM pending_resume WHERE obligation_id = ?", (obligation_id,)
            )
        return dict(row) if row else None
