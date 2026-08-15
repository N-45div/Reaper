"""Deterministic notice-deadline derivation from raw clause text.

The LLM extracts a renewal clause and proposes a notice deadline; this engine
independently re-derives the deadline with regex + calendar math. Any
disagreement (or any clause this engine cannot parse unambiguously) BLOCKS
scheduling — a hallucinated date must never become a missed deadline.
"""

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from dateutil.relativedelta import relativedelta

_WORD_NUMBERS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "fifty": 50,
    "sixty": 60, "ninety": 90, "one hundred twenty": 120, "one hundred eighty": 180,
}

_UNITS = {"day": "days", "days": "days", "week": "weeks", "weeks": "weeks",
          "month": "months", "months": "months"}

# "sixty (60) days" | "60 days" | "ninety days" | "three (3) months"
_WORD_ALT = "|".join(
    sorted((re.escape(w) for w in _WORD_NUMBERS), key=len, reverse=True)
)
_PERIOD_RE = re.compile(
    rf"(?:\b(?P<word>{_WORD_ALT})\s*)?"
    r"(?:\((?P<digits_paren>\d{1,3})\)\s*)?"
    r"(?:(?P<digits>\d{1,3})\s*)?"
    r"(?P<unit>days?|weeks?|months?)\b",
    re.IGNORECASE,
)

_ANCHOR_PATTERNS = [
    (r"prior to the (?:end|expiration) of the(?: then[- ]current)? (?:term|period)", "term_end"),
    (r"before the (?:end|expiration) of the(?: then[- ]current)? (?:term|period)", "term_end"),
    (r"prior to (?:the )?(?:renewal|anniversary) date", "term_end"),
    (r"before (?:the )?(?:renewal|anniversary) date", "term_end"),
    (r"prior to (?:the )?expiration", "term_end"),
    (r"before (?:such |the )?renewal", "term_end"),
]


@dataclass
class Derivation:
    status: str                    # DERIVED | AMBIGUOUS
    deadline: date | None = None
    period_value: int | None = None
    period_unit: str | None = None
    anchor: str | None = None
    reasons: list[str] = field(default_factory=list)
    trace: list[str] = field(default_factory=list)


def _word_to_number(phrase: str) -> int | None:
    return _WORD_NUMBERS.get(phrase.strip().lower().replace("  ", " "))


def _find_periods(text: str) -> tuple[list[tuple[int, str]], list[str]]:
    """All (value, unit) notice periods mentioned; ambiguity reasons if any."""
    periods: list[tuple[int, str]] = []
    reasons: list[str] = []
    for m in _PERIOD_RE.finditer(text):
        unit = _UNITS[m.group("unit").lower()]
        word_val = _word_to_number(m.group("word") or "")
        paren_val = int(m.group("digits_paren")) if m.group("digits_paren") else None
        digit_val = int(m.group("digits")) if m.group("digits") else None

        candidates = {v for v in (word_val, paren_val, digit_val) if v is not None}
        if not candidates:
            continue
        # A duration immediately naming the renewal term ("twelve (12) month
        # terms", "successive 6 month periods") is the term length, not the
        # notice period — skip it.
        if re.match(r"\s*(?:terms?|periods?)\b", text[m.end():], re.IGNORECASE):
            continue
        if len(candidates) > 1:
            reasons.append(
                f"conflicting written and numeric values in '{m.group(0).strip()}'"
            )
            continue
        periods.append((candidates.pop(), unit))
    return periods, reasons


def derive_deadline(clause_text: str, term_end: date) -> Derivation:
    """Re-derive the notice deadline for a term ending `term_end`."""
    d = Derivation(status="AMBIGUOUS")
    text = " ".join(clause_text.split())
    d.trace.append(f"normalized clause ({len(text)} chars)")

    if re.search(r"business\s+days?", text, re.IGNORECASE):
        d.reasons.append("business-day notice periods are not supported; needs human review")
        return d

    periods, period_reasons = _find_periods(text)
    d.reasons.extend(period_reasons)
    unique = sorted(set(periods))
    d.trace.append(f"periods found: {unique or 'none'}")

    if period_reasons:
        return d
    if not unique:
        d.reasons.append("no notice period found in clause")
        return d
    if len(unique) > 1:
        d.reasons.append(f"multiple distinct notice periods found: {unique}")
        return d

    anchor = None
    for pattern, anchor_kind in _ANCHOR_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            anchor = anchor_kind
            break
    d.trace.append(f"anchor: {anchor}")
    if anchor is None:
        d.reasons.append("no recognizable deadline anchor phrase (e.g. 'prior to the end of the term')")
        return d

    value, unit = unique[0]
    if unit == "days":
        deadline = term_end - timedelta(days=value)
    elif unit == "weeks":
        deadline = term_end - timedelta(weeks=value)
    else:
        deadline = term_end - relativedelta(months=value)

    d.status = "DERIVED"
    d.deadline = deadline
    d.period_value = value
    d.period_unit = unit
    d.anchor = anchor
    d.trace.append(f"deadline = {term_end} - {value} {unit} = {deadline}")
    return d


def gate(llm_deadline: date, clause_text: str, term_end: date) -> tuple[str, Derivation]:
    """The scheduling gate. Returns (verdict, derivation).

    MATCH    -> safe to schedule
    MISMATCH -> LLM and engine disagree; scheduling BLOCKED
    AMBIGUOUS-> engine cannot independently verify; scheduling BLOCKED
    """
    derivation = derive_deadline(clause_text, term_end)
    if derivation.status == "AMBIGUOUS":
        return "AMBIGUOUS", derivation
    if derivation.deadline != llm_deadline:
        derivation.reasons.append(
            f"LLM proposed {llm_deadline}, engine derived {derivation.deadline}"
        )
        return "MISMATCH", derivation
    return "MATCH", derivation
