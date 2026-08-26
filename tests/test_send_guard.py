"""The product's one non-negotiable: no notice leaves without a human on record.

A degraded model, a fallback model, or a resumed run must all be mechanically
unable to deliver a notice that no human approved - the instruction asks, but
this guard refuses.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reaper.ledger_sqlite as ledger_sqlite
from reaper import inbox, tools


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_sqlite, "LEDGER_PATH", tmp_path / "ledger.db")
    monkeypatch.setattr(tools, "ledger", ledger_sqlite)
    monkeypatch.setattr(inbox, "OUTBOX", tmp_path / "outbox")


def _obligation(status):
    return ledger_sqlite.create_obligation(
        vendor="GuardCo", contract_file=None,
        clause_text="renews automatically unless ninety (90) days notice",
        term_end="2027-02-28", llm_deadline="2026-11-30",
        engine_deadline="2026-11-30", gate_verdict="MATCH",
        status=status, notice_method="email",
        recipient="legal@guardco.test", expected_final_amount=0.0,
    )


def test_send_notice_refused_without_approval(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = _obligation("AWAITING_APPROVAL")

    out = tools.send_notice(obligation_id=oid, notice_text="We elect not to renew.")

    assert "error" in out and "refused" in out["error"]
    kinds = [r["kind"] for r in ledger_sqlite.get_receipts(oid)]
    assert "NOTICE_SENT" not in kinds


def test_send_notice_allowed_after_approval(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = _obligation("AWAITING_APPROVAL")
    ledger_sqlite.append_receipt(oid, "APPROVED", {"channel": "test"})

    out = tools.send_notice(obligation_id=oid, notice_text="We elect not to renew.")

    assert out.get("delivered") is True
    kinds = [r["kind"] for r in ledger_sqlite.get_receipts(oid)]
    assert "NOTICE_SENT" in kinds
