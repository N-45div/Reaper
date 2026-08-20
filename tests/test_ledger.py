import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reaper.ledger_sqlite as ledger


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ledger.db")


def test_chain_intact(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = ledger.create_obligation(
        vendor="CloudCo", contract_file="c.pdf", clause_text="60 days",
        term_end="2026-12-31", llm_deadline="2026-11-01",
        engine_deadline="2026-11-01", gate_verdict="MATCH",
        status="SCHEDULED", notice_method="email",
        recipient="cancel@cloudco.test", expected_final_amount=0.0,
    )
    ledger.append_receipt(oid, "EXTRACTED", {"clause": "60 days"})
    ledger.append_receipt(oid, "GATED", {"verdict": "MATCH"})
    ledger.append_receipt(oid, "NOTICE_SENT", {"to": "cancel@cloudco.test"})
    intact, broken = ledger.verify_chain(oid)
    assert intact and broken is None


def test_tamper_breaks_chain(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = ledger.create_obligation(
        vendor="X", contract_file=None, clause_text="c", term_end="2026-12-31",
        llm_deadline=None, engine_deadline=None, gate_verdict="MATCH",
        status="SCHEDULED", notice_method=None, recipient=None,
        expected_final_amount=None,
    )
    ledger.append_receipt(oid, "EXTRACTED", {"a": 1})
    ledger.append_receipt(oid, "GATED", {"b": 2})
    with ledger._connect() as conn:
        conn.execute(
            "UPDATE receipts SET payload = '{\"a\": 999}' WHERE obligation_id = ? AND kind = 'EXTRACTED'",
            (oid,),
        )
    intact, _ = ledger.verify_chain(oid)
    assert not intact


def test_resume_pointer_roundtrip(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = ledger.create_obligation(
        vendor="X", contract_file=None, clause_text="c", term_end="2026-12-31",
        llm_deadline=None, engine_deadline=None, gate_verdict="MATCH",
        status="AWAITING_APPROVAL", notice_method=None, recipient=None,
        expected_final_amount=None,
    )
    ledger.save_resume_pointer(oid, "u1", "s1", "inv1", "fc1")
    ptr = ledger.get_resume_pointer(oid)
    assert ptr["invocation_id"] == "inv1"
    assert ptr["function_call_id"] == "fc1"

    # A failed resume must not consume the pointer: reading it twice still works.
    assert ledger.get_resume_pointer(oid) is not None

    ledger.clear_resume_pointer(oid)
    assert ledger.get_resume_pointer(oid) is None
