"""Stub vendor world: outbound notice delivery + inbound invoice feed.

Real integrations (email, payment rails) are deliberately out of scope; the
demo's honesty lives in what IS real — extraction, the date gate, the durable
pause, the restart, the hash chain. Vendors are scripted personas:
  - CloudCo Metrics honors a valid cancellation (final invoice prorated to $0 next cycle)
  - DataVault Pro ignores it and bills the full amount anyway (the villain)
"""

import json
from datetime import date

from .config import DATA_DIR

OUTBOX = DATA_DIR / "outbox"          # delivered notices land here as files
INVOICES = DATA_DIR / "invoices"      # seeded next-cycle invoices per vendor

VENDORS = {
    "CloudCo Metrics": {"honors_cancellation": True,  "cycle_amount": 299.0},
    "DataVault Pro":   {"honors_cancellation": False, "cycle_amount": 540.0},
}


def deliver_notice(vendor: str, notice_text: str, obligation_id: int) -> dict:
    """'Send' the cancellation notice; returns a delivery record."""
    OUTBOX.mkdir(parents=True, exist_ok=True)
    path = OUTBOX / f"notice-{obligation_id}-{vendor.replace(' ', '_')}.txt"
    path.write_text(notice_text, encoding="utf-8")
    return {
        "delivered_to": f"cancellations@{vendor.replace(' ', '').lower()}.test",
        "delivery_path": str(path),
        "delivered_on": date.today().isoformat(),
    }


def seed_next_invoice(vendor: str, notice_was_valid: bool) -> dict:
    """Generate the next-cycle invoice according to the vendor's persona."""
    persona = VENDORS[vendor]
    if notice_was_valid and persona["honors_cancellation"]:
        amount = 0.0
        memo = "Subscription cancelled per notice received. No further charges."
    else:
        amount = persona["cycle_amount"]
        memo = "Renewal charge for the upcoming service period."
    INVOICES.mkdir(parents=True, exist_ok=True)
    invoice = {
        "vendor": vendor,
        "amount": amount,
        "memo": memo,
        "invoice_date": date.today().isoformat(),
    }
    path = INVOICES / f"invoice-{vendor.replace(' ', '_')}.json"
    path.write_text(json.dumps(invoice, indent=2), encoding="utf-8")
    return invoice


def read_next_invoice(vendor: str) -> dict | None:
    path = INVOICES / f"invoice-{vendor.replace(' ', '_')}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
