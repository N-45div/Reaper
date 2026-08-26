"""Filing the same contract twice must never create a twin obligation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reaper.ledger_sqlite as ledger_sqlite
from reaper import tools


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_sqlite, "LEDGER_PATH", tmp_path / "ledger.db")
    monkeypatch.setattr(tools, "ledger", ledger_sqlite)


CLAUSE = ("Upon expiration of the Initial Term, this Agreement will renew "
          "automatically unless Client delivers written notice of termination "
          "no later than ninety (90) days prior to the expiration.")


def _file_once():
    return tools.gate_and_schedule(
        vendor="DataVault Pro", clause_text=CLAUSE, term_end="2027-02-28",
        proposed_deadline="2026-11-30", recipient="accounts@datavaultpro.test",
        expected_final_amount=0.0)


def test_second_filing_is_deduplicated(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    first = _file_once()
    second = _file_once()

    assert second["duplicate"] is True
    assert second["obligation_id"] == first["obligation_id"]
    assert len(ledger_sqlite.list_obligations()) == 1
    kinds = [r["kind"] for r in ledger_sqlite.get_receipts(first["obligation_id"])]
    assert "DUPLICATE_FILING_IGNORED" in kinds


def test_completed_filing_allows_a_new_one(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    first = _file_once()
    ledger_sqlite.set_status(first["obligation_id"], "NOTICE_SENT")

    second = _file_once()

    assert "duplicate" not in second
    assert second["obligation_id"] != first["obligation_id"]
