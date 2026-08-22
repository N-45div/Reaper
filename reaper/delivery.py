"""Delivery-method classification: HOW the contract says notice must travel.

Notice clauses are construed strictly. A clause demanding registered post is
not satisfied by an email the vendor demonstrably read — so an agent that
emails a notice and calls the obligation handled would be shipping an
overclaim. This classifier is deterministic (regex over the clause text, no
model) so the resulting method can carry the same weight as the date gate.
"""

import re
from dataclasses import dataclass

# Most restrictive first: when a clause names several channels without an
# "or", the physical one governs.
_METHODS: list[tuple[str, re.Pattern]] = [
    ("REGISTERED_POST", re.compile(r"registered (?:post|mail|a\.?d\.?)|speed post", re.I)),
    ("CERTIFIED_MAIL", re.compile(r"certified mail|certified post", re.I)),
    ("COURIER", re.compile(r"(?:by|via|through)\s+(?:a\s+)?(?:reputable\s+|overnight\s+|recognised\s+|recognized\s+)?courier|overnight delivery service", re.I)),
    ("PORTAL", re.compile(r"(?:via|through|using)\s+(?:the\s+)?(?:vendor|customer|supplier|online|account|billing)?\s*portal|account dashboard", re.I)),
    ("EMAIL", re.compile(r"e-?mail|electronic mail|electronically", re.I)),
]

# An address in the clause is itself evidence that email is an accepted channel
# ("notice ... delivered in writing to cancellations@vendor.test").
_EMAIL_ADDR = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_OR_JOIN = re.compile(r"\bor\b", re.I)


@dataclass
class DeliveryRuling:
    method: str            # strictest channel the clause names, or UNSPECIFIED
    email_compliant: bool  # may a compliant notice travel by email?
    evidence: str          # the phrase that decided it, for the record


def classify(clause_text: str) -> DeliveryRuling:
    text = " ".join((clause_text or "").split())
    named: list[tuple[str, str]] = []
    for method, pattern in _METHODS:
        m = pattern.search(text)
        if m:
            named.append((method, m.group(0)))

    has_addr = bool(_EMAIL_ADDR.search(text))
    email_named = any(m == "EMAIL" for m, _ in named) or has_addr
    physical = [(m, ev) for m, ev in named if m in
                ("REGISTERED_POST", "CERTIFIED_MAIL", "COURIER")]

    if physical:
        method, evidence = physical[0]
        # "by email or by registered post" — alternatives make email compliant.
        alternatives = email_named and _OR_JOIN.search(text) is not None
        return DeliveryRuling(method=method,
                              email_compliant=alternatives,
                              evidence=evidence)
    if email_named:
        ev = next((ev for m, ev in named if m == "EMAIL"), None)
        addr = _EMAIL_ADDR.search(text)
        return DeliveryRuling(method="EMAIL", email_compliant=True,
                              evidence=ev or (addr.group(0) if addr else "email"))
    if named:  # portal only
        return DeliveryRuling(method=named[0][0], email_compliant=False,
                              evidence=named[0][1])
    return DeliveryRuling(method="UNSPECIFIED", email_compliant=True,
                          evidence="no delivery method named; written notice assumed deliverable by email")
