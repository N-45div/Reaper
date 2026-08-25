import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reaper.ledger_sqlite as ledger_sqlite
from reaper import inbox, tools


def _fresh(tmp_path, monkeypatch):
    """Point the tools module at a throwaway sqlite ledger and outbox."""
    monkeypatch.setattr(ledger_sqlite, "LEDGER_PATH", tmp_path / "ledger.db")
    monkeypatch.setattr(tools, "ledger", ledger_sqlite)
    monkeypatch.setattr(inbox, "OUTBOX", tmp_path / "outbox")


def _obligation(status):
    return ledger_sqlite.create_obligation(
        vendor="DataVault Pro", contract_file=None, clause_text="ninety (90) days",
        term_end="2027-02-28", llm_deadline="2026-11-30",
        engine_deadline="2026-11-30", gate_verdict="MATCH",
        status=status, notice_method="email",
        recipient="accounts@datavaultpro.test", expected_final_amount=0.0,
    )


def test_backstop_files_the_dispute_when_the_run_ended_early(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = _obligation("REFUTED")
    ledger_sqlite.append_receipt(oid, "NOTICE_SENT", {"to": "accounts@datavaultpro.test"})
    ledger_sqlite.append_receipt(oid, "INVOICE_CHECKED", {"billed": 540.0, "expected": 0})

    result = tools.ensure_dispute_filed(oid)

    assert result is not None and result["disputed"] is True
    assert ledger_sqlite.get_obligation(oid)["status"] == "DISPUTED"
    kinds = [r["kind"] for r in ledger_sqlite.get_receipts(oid)]
    assert "DISPUTE_OPENED" in kinds
    # The chain must say who filed it: the backstop, not the model.
    assert "DISPUTE_BACKSTOP" in kinds
    intact, broken = ledger_sqlite.verify_chain(oid)
    assert intact and broken is None


def test_backstop_never_files_twice(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = _obligation("REFUTED")
    ledger_sqlite.append_receipt(oid, "NOTICE_SENT", {"to": "accounts@datavaultpro.test"})
    ledger_sqlite.append_receipt(oid, "DISPUTE_OPENED", {"evidence_hash": "deadbeef"})

    assert tools.ensure_dispute_filed(oid) is None
    kinds = [r["kind"] for r in ledger_sqlite.get_receipts(oid)]
    assert kinds.count("DISPUTE_OPENED") == 1
    assert "DISPUTE_BACKSTOP" not in kinds


def test_backstop_ignores_a_verified_obligation(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = _obligation("VERIFIED")
    ledger_sqlite.append_receipt(oid, "NOTICE_SENT", {"to": "accounts@datavaultpro.test"})

    assert tools.ensure_dispute_filed(oid) is None
    assert ledger_sqlite.get_obligation(oid)["status"] == "VERIFIED"


def test_backstop_refuses_to_dispute_without_a_delivery_receipt(tmp_path, monkeypatch):
    """No proof of service, no dispute — the evidence is the whole point."""
    _fresh(tmp_path, monkeypatch)
    oid = _obligation("REFUTED")
    ledger_sqlite.append_receipt(oid, "INVOICE_CHECKED", {"billed": 540.0, "expected": 0})

    assert tools.ensure_dispute_filed(oid) is None
    assert ledger_sqlite.get_obligation(oid)["status"] == "REFUTED"
