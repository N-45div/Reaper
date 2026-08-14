import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.date_engine import derive_deadline, gate

TERM_END = date(2026, 12, 31)


def test_digits_days():
    d = derive_deadline(
        "either party may terminate with 60 days written notice prior to the end of the then-current term",
        TERM_END,
    )
    assert d.status == "DERIVED"
    assert d.deadline == date(2026, 11, 1)


def test_word_and_paren_agree():
    d = derive_deadline(
        "no later than sixty (60) days prior to the end of the then-current term",
        TERM_END,
    )
    assert d.status == "DERIVED"
    assert d.deadline == date(2026, 11, 1)


def test_word_only():
    d = derive_deadline(
        "at least ninety days before the renewal date",
        TERM_END,
    )
    assert d.status == "DERIVED"
    assert d.deadline == date(2026, 10, 2)


def test_months_unit():
    d = derive_deadline(
        "three (3) months written notice prior to the expiration of the term",
        TERM_END,
    )
    assert d.status == "DERIVED"
    assert d.deadline == date(2026, 9, 30)


def test_word_paren_conflict_is_ambiguous():
    # The planted demo case: words say sixty, digits say ninety.
    d = derive_deadline(
        "no later than sixty (90) days prior to the end of the then-current term",
        TERM_END,
    )
    assert d.status == "AMBIGUOUS"
    assert any("conflicting" in r for r in d.reasons)


def test_multiple_periods_ambiguous():
    d = derive_deadline(
        "30 days notice for monthly plans and 60 days notice for annual plans, prior to the end of the term",
        TERM_END,
    )
    assert d.status == "AMBIGUOUS"


def test_business_days_ambiguous():
    d = derive_deadline(
        "ten (10) business days prior to the end of the term",
        TERM_END,
    )
    assert d.status == "AMBIGUOUS"


def test_no_anchor_ambiguous():
    d = derive_deadline("60 days written notice is required", TERM_END)
    assert d.status == "AMBIGUOUS"


def test_gate_match():
    verdict, _ = gate(
        date(2026, 11, 1),
        "60 days notice prior to the end of the then-current term",
        TERM_END,
    )
    assert verdict == "MATCH"


def test_gate_mismatch_blocks():
    verdict, d = gate(
        date(2026, 11, 15),  # hallucinated date
        "60 days notice prior to the end of the then-current term",
        TERM_END,
    )
    assert verdict == "MISMATCH"
    assert any("LLM proposed" in r for r in d.reasons)
