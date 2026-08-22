"""Redaction: what the models are allowed to see.

An agent that reads a mailbox to find contracts will inevitably brush against
things that are none of its business — a card number in a payment confirmation,
a PAN in a tax email, a phone number in a signature. None of that is needed to
work out when a renewal notice is due, so none of it should leave the machine.

Everything here is deterministic and checksum-validated where a checksum exists,
so redaction can be unit-tested and does not itself depend on a model. Findings
are counted and recorded, which lets the ledger state plainly what was masked
without storing the values that were masked.
"""

import re
from dataclasses import dataclass, field


@dataclass
class Redaction:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def summary(self) -> str:
        if not self.counts:
            return "nothing redacted"
        return ", ".join(f"{n} {kind}" for kind, n in sorted(self.counts.items()))


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6], [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8], [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2], [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4], [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]
_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9], [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2], [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0], [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5], [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def _verhoeff(digits: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(digits)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][ord(ch) - 48]]
    return c == 0


def _mask_tail(value: str, keep: int = 4) -> str:
    tail = value[-keep:] if len(value) > keep else ""
    return f"[redacted:{tail}]" if tail else "[redacted]"


# Order matters: the most specific patterns run first so a PAN is never eaten
# by a looser numeric rule.
_PATTERNS: list[tuple[str, re.Pattern, object]] = [
    ("PAN", re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b"), None),
    ("GSTIN", re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]{3}\b"), None),
    ("IFSC", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b"), None),
    ("IBAN", re.compile(r"\b[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}\b"), None),
    ("card number", re.compile(r"\b(?:\d[ -]?){13,19}\b"), "luhn"),
    ("Aadhaar number", re.compile(r"\b[2-9][0-9]{3}[ -]?[0-9]{4}[ -]?[0-9]{4}\b"), "verhoeff"),
    ("passport number", re.compile(r"\b[A-PR-WY][0-9]{7}\b"), None),
    ("phone number", re.compile(r"(?<![\d/-])(?:\+?\d{1,3}[ -]?)?(?:\d[ -]?){9,12}\d(?![\d/-])"), "phone"),
]


def _accept(kind: str, raw: str, check) -> bool:
    digits = re.sub(r"\D", "", raw)
    if check == "luhn":
        return 13 <= len(digits) <= 19 and _luhn(digits)
    if check == "verhoeff":
        return len(digits) == 12 and _verhoeff(digits)
    if check == "phone":
        # Long enough to be a subscriber number, and not a date or an amount.
        return 10 <= len(digits) <= 13
    return True


def redact(text: str, keep_emails: bool = False) -> Redaction:
    """Mask identifiers the agent has no business reading.

    Contract terms, dates, vendor names and amounts are deliberately untouched:
    those are the substance of the work. What goes is anything that identifies a
    person or a payment instrument.

    keep_emails=True is the CONTRACT profile: a notice clause's whole point is
    an address ("written notice to cancellations@vendor.test"), and the agent
    cannot serve notice on a masked recipient. Identity and payment numbers are
    masked in every profile; only the email rule is purpose-dependent.
    """
    counts: dict[str, int] = {}
    if not text:
        return Redaction(text="", counts=counts)

    out = text
    for kind, pattern, check in _PATTERNS:
        def replace(m: re.Match, _kind=kind, _check=check) -> str:
            raw = m.group(0)
            if not _accept(_kind, raw, _check):
                return raw
            counts[_kind] = counts.get(_kind, 0) + 1
            return _mask_tail(re.sub(r"\D", "", raw) or raw)

        out = pattern.sub(replace, out)

    if not keep_emails:
        # Keep the domain, which is often the vendor's identity and matters to
        # the obligation, but drop the person.
        def hide_local(m: re.Match) -> str:
            counts["email address"] = counts.get("email address", 0) + 1
            return f"[redacted]@{m.group(2)}"

        out = re.sub(r"\b([A-Za-z0-9._%+-]+)@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b",
                     hide_local, out)
    return Redaction(text=out, counts=counts)
