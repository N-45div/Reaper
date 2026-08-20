"""Stub vendor world: outbound notice delivery + inbound invoice documents.

Real integrations (email, payment rails) are deliberately out of scope; the
demo's honesty lives in what IS real — extraction, the date gate, the durable
pause, the restart, the hash chain, and the fact that the agent has to READ the
invoice document rather than being handed a tidy number.

Vendors are scripted personas:
  - CloudCo Metrics honors a valid cancellation (final invoice at zero)
  - DataVault Pro ignores it and bills the full amount anyway (the villain)
  - anyone else honors it, so an uploaded contract behaves sensibly
"""

import json
from datetime import date, timedelta
from pathlib import Path

from .config import DATA_DIR
from .invoice_doc import render_invoice

OUTBOX = DATA_DIR / "outbox"          # delivered notices land here as files
INVOICES = DATA_DIR / "invoices"      # rendered next-cycle invoice documents

VENDORS = {
    "CloudCo Metrics": {"honors_cancellation": True,  "cycle_amount": 299.0, "currency": "USD"},
    "DataVault Pro":   {"honors_cancellation": False, "cycle_amount": 540.0, "currency": "USD"},
    "Northwind Facilities Ltd.": {"honors_cancellation": True, "cycle_amount": 96000.0, "currency": "INR"},
}
DEFAULT_PERSONA = {"honors_cancellation": True, "cycle_amount": 250.0, "currency": "USD"}


def persona(vendor: str) -> dict:
    for name, p in VENDORS.items():
        if name.lower() in vendor.lower() or vendor.lower() in name.lower():
            return p
    return DEFAULT_PERSONA


def deliver_notice(vendor: str, notice_text: str, obligation_id: int) -> dict:
    """'Send' the cancellation notice; returns a delivery record."""
    OUTBOX.mkdir(parents=True, exist_ok=True)
    path = OUTBOX / f"notice-{obligation_id}-{_slug(vendor)}.txt"
    path.write_text(notice_text, encoding="utf-8")
    return {
        "delivered_to": f"cancellations@{_slug(vendor).lower()}.test",
        "delivery_path": str(path),
        "delivered_on": date.today().isoformat(),
    }


def _slug(vendor: str) -> str:
    return "".join(c for c in vendor.replace(" ", "_") if c.isalnum() or c == "_")


def invoice_path(vendor: str) -> Path:
    return INVOICES / f"invoice-{_slug(vendor)}.jpg"


def seed_next_invoice(vendor: str, notice_was_valid: bool, term_end: str | None = None) -> dict:
    """Issue the next-cycle invoice as a document the agent will have to read.

    The returned dict is the simulated world's own record. Nothing downstream
    trusts it: the agent reads the rendered document instead.
    """
    p = persona(vendor)
    honored = notice_was_valid and p["honors_cancellation"]
    amount = 0.0 if honored else p["cycle_amount"]
    memo = ("Subscription cancelled per notice received. No further charges."
            if honored else "Renewal charge for the upcoming service period")
    when = date.today().isoformat()
    if term_end:
        try:
            when = (date.fromisoformat(term_end) + timedelta(days=1)).isoformat()
        except ValueError:
            pass
    doc = render_invoice(vendor, amount, memo, when, currency=p["currency"])
    record = {"vendor": vendor, "amount": amount, "currency": p["currency"],
              "memo": memo, "invoice_date": when, "document": str(doc)}
    INVOICES.mkdir(parents=True, exist_ok=True)
    (INVOICES / f"invoice-{_slug(vendor)}.json").write_text(
        json.dumps(record, indent=2), encoding="utf-8")
    return record


def read_next_invoice(vendor: str) -> dict | None:
    """The world's own record of the issued invoice (not what the agent reads)."""
    path = INVOICES / f"invoice-{_slug(vendor)}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def reset_world() -> None:
    """Demo reset: clear delivered notices and issued invoices."""
    import shutil
    for d in (OUTBOX, INVOICES):
        shutil.rmtree(d, ignore_errors=True)
