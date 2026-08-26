import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper import precedent, tools


def _match(**kw):
    base = {
        "clause_id": "fixture:X:abc", "source": "fixture", "obligation_id": None,
        "vendor": "CloudCo Metrics", "same_vendor": False,
        "clause_snippet": "sixty (60) days prior to the end of the term",
        "gate_verdict": "MATCH", "gate_reason": None, "status_final": "VERIFIED",
        "notice_period": "60 days", "anchor": "term_end", "notice_method": "EMAIL",
        "billing_stopped": True, "dispute_opened": None,
        "resolution": "fixture corpus", "terminal_receipt_hash": None,
        "similarity": 0.9,
    }
    base.update(kw)
    return base


# --- pure helpers ---------------------------------------------------------

def test_clause_id_is_stable_and_content_addressed():
    a = precedent.clause_id("CloudCo", "Sixty (60)  days\nnotice", "fixture")
    b = precedent.clause_id("CloudCo", "sixty (60) days notice", "fixture")
    c = precedent.clause_id("CloudCo", "ninety (90) days notice", "fixture")
    assert a == b
    assert a != c
    assert a.startswith("fixture:CloudCo:")


def test_build_row_matches_schema_exactly():
    row = precedent.build_row(
        clause_id="fixture:X:abc", source="fixture", vendor="X",
        clause_text="sixty (60) days", engine_deadline="2026-11-01",
        term_end="2026-12-31", gate_verdict="MATCH",
    )
    schema_cols = {c["name"] for c in json.loads(
        (Path(__file__).resolve().parent.parent / "data" / "precedents" /
         "schema.json").read_text())}
    assert set(row) <= schema_cols
    assert row["engine_deadline"] == "2026-11-01"
    with pytest.raises(KeyError):
        precedent.build_row(clause_id="x", source="fixture", not_a_column=1)


def test_summarize_drops_below_threshold():
    out = precedent.summarize(
        [_match(similarity=0.95, clause_id="a"),
         _match(similarity=0.75, clause_id="b"),
         _match(similarity=0.40, clause_id="c")],
        min_similarity=0.72)
    sims = [m["similarity"] for m in out["matches"]]
    assert sims == [0.95, 0.75]


def test_summarize_warns_on_blocked_precedent():
    hot = _match(similarity=0.91, gate_verdict="AMBIGUOUS",
                 gate_reason="words disagree with numerals")
    out = precedent.summarize([hot])
    assert out["warning"] and "prior history, not a verdict" in out["warning"]
    cold = _match(similarity=0.74, gate_verdict="AMBIGUOUS")
    assert precedent.summarize([cold])["warning"] is None


def test_summarize_warns_on_refuted_precedent():
    m = _match(similarity=0.85, gate_verdict="MATCH", status_final="REFUTED",
               dispute_opened=True, billing_stopped=False)
    out = precedent.summarize([m])
    assert out["warning"] and "billed anyway" in out["warning"]


# --- fail-open behaviour --------------------------------------------------

def _boom(*a, **k):
    raise AssertionError("must never be reached")


def test_recall_disabled_is_a_pure_noop(monkeypatch):
    monkeypatch.setattr(precedent, "PRECEDENT_BACKEND", "off")
    monkeypatch.setattr(precedent, "_client", _boom)
    monkeypatch.setattr(precedent, "embed_clause", _boom)
    out = precedent.recall(vendor="X", clause_text="sixty (60) days")
    assert out["available"] is False and out["reason"] == "disabled"


def test_recall_fails_open_on_backend_error(monkeypatch):
    monkeypatch.setattr(precedent, "PRECEDENT_BACKEND", "bigquery")
    monkeypatch.setattr(precedent, "embed_clause", lambda *a, **k: [0.1, 0.2])
    monkeypatch.setattr(precedent, "_client",
                        lambda: (_ for _ in ()).throw(RuntimeError("403")))
    out = precedent.recall(vendor="X", clause_text="sixty (60) days")
    assert out["available"] is False
    assert out["reason"] == "RuntimeError"
    assert out["matches"] == []


def test_recall_fails_open_on_embedding_error(monkeypatch):
    monkeypatch.setattr(precedent, "PRECEDENT_BACKEND", "bigquery")
    monkeypatch.setattr(precedent, "embed_clause",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("quota")))
    monkeypatch.setattr(precedent, "_client", _boom)
    out = precedent.recall(vendor="X", clause_text="sixty (60) days")
    assert out["available"] is False and out["reason"] == "ValueError"


# --- the boundary: history advises, it never rules ------------------------

class _FakeLedger:
    def __init__(self):
        self.receipts = []
        self.obligations = {}
        self._next = 0

    def create_obligation(self, **kw):
        self._next += 1
        self.obligations[self._next] = dict(kw, id=self._next)
        return self._next

    def append_receipt(self, oid, kind, payload):
        self.receipts.append({"obligation_id": oid, "kind": kind,
                              "payload": payload})
        return self.receipts[-1]

    def get_obligation(self, oid):
        return self.obligations.get(oid)

    def list_obligations(self):
        return list(self.obligations.values())

    def set_status(self, oid, status):
        self.obligations[oid]["status"] = status

    def get_receipts(self, oid):
        return [r for r in self.receipts if r["obligation_id"] == oid]


CLEAN_CLAUSE = ("This Agreement shall automatically renew for successive "
                "twelve (12) month terms unless either party provides written "
                "notice of non-renewal at least sixty (60) days prior to the "
                "end of the then-current term.")


def _scary_precedent(**kw):
    return {
        "available": True, "reason": None, "backend": "bigquery",
        "embedding_model": "gemini-embedding-001", "embedding_dim": 768,
        "rows_scanned": 5, "latency_ms": 40,
        "matches": [_match(similarity=0.97, gate_verdict="AMBIGUOUS",
                           gate_reason="words disagree with numerals")],
        "summary": "1 prior clause matched (best 0.97). Advisory only.",
        "warning": ("a near-identical clause was BLOCKED before. "
                    "This is prior history, not a verdict."),
        **kw,
    }


def test_precedent_never_changes_a_verdict(monkeypatch):
    fake = _FakeLedger()
    monkeypatch.setattr(tools, "ledger", fake)
    monkeypatch.setattr(tools.precedent, "recall",
                        lambda **kw: _scary_precedent())
    result = tools.gate_and_schedule(
        vendor="CloudCo Metrics", clause_text=CLEAN_CLAUSE,
        term_end="2026-12-31", proposed_deadline="2026-11-01",
        recipient="cancellations@cloudcometrics.test",
        expected_final_amount=0.0)
    assert result["gate_verdict"] == "MATCH"
    assert result["status"] == "SCHEDULED"
    assert "precedent_warning" in result


def test_receipt_order_and_no_vectors(monkeypatch):
    fake = _FakeLedger()
    monkeypatch.setattr(tools, "ledger", fake)
    monkeypatch.setattr(tools.precedent, "recall",
                        lambda **kw: _scary_precedent())
    tools.gate_and_schedule(
        vendor="CloudCo Metrics", clause_text=CLEAN_CLAUSE,
        term_end="2026-12-31", proposed_deadline="2026-11-01",
        recipient="cancellations@cloudcometrics.test",
        expected_final_amount=0.0)
    kinds = [r["kind"] for r in fake.receipts]
    assert kinds == ["EXTRACTED", "PRECEDENT_CONSULTED", "GATED"]
    pc = next(r for r in fake.receipts if r["kind"] == "PRECEDENT_CONSULTED")
    blob = json.loads(json.dumps(pc["payload"], default=str))

    def no_long_float_lists(node):
        if isinstance(node, list):
            floats = [x for x in node if isinstance(x, float)]
            assert len(floats) <= 8
            for x in node:
                no_long_float_lists(x)
        elif isinstance(node, dict):
            for v in node.values():
                no_long_float_lists(v)

    no_long_float_lists(blob)


def test_unavailable_precedent_is_still_receipted(monkeypatch):
    fake = _FakeLedger()
    monkeypatch.setattr(tools, "ledger", fake)
    monkeypatch.setattr(
        tools.precedent, "recall",
        lambda **kw: dict(precedent._unavailable("ConnectionError")))
    result = tools.gate_and_schedule(
        vendor="CloudCo Metrics", clause_text=CLEAN_CLAUSE,
        term_end="2026-12-31", proposed_deadline="2026-11-01",
        recipient="cancellations@cloudcometrics.test",
        expected_final_amount=0.0)
    pc = next(r for r in fake.receipts if r["kind"] == "PRECEDENT_CONSULTED")
    assert pc["payload"]["available"] is False
    assert "unavailable" in result["precedent"]
    assert "ConnectionError" in result["precedent"]


def test_bigquery_is_not_imported_at_module_scope():
    src = (Path(__file__).resolve().parent.parent / "reaper" /
           "precedent.py").read_text(encoding="utf-8")
    assert "\nfrom google.cloud import bigquery" not in src
