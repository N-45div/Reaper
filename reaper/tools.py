"""Agent-facing tools. The agent orchestrates; every verdict that matters
(deadline gate, invoice verdict, chain hashes) is computed here in plain code."""

from datetime import date

from . import inbox, ledger
from .date_engine import gate


def gate_and_schedule(
    vendor: str,
    clause_text: str,
    term_end: str,
    proposed_deadline: str,
    recipient: str,
    expected_final_amount: float,
) -> dict:
    """Validate the extracted renewal clause and schedule the obligation.

    Args:
        vendor: Vendor / counterparty name as written in the contract.
        clause_text: The auto-renewal clause verbatim from the contract.
        term_end: Current term end date, ISO format YYYY-MM-DD.
        proposed_deadline: The notice deadline you derived, ISO YYYY-MM-DD.
        recipient: Where the contract says notice must be sent.
        expected_final_amount: Amount the next invoice should show if the
            cancellation is honored (usually 0.0).

    Returns dict with gate verdict (MATCH schedules; MISMATCH/AMBIGUOUS blocks).
    """
    term_end_d = date.fromisoformat(term_end)
    proposed_d = date.fromisoformat(proposed_deadline)
    verdict, derivation = gate(proposed_d, clause_text, term_end_d)

    status = "SCHEDULED" if verdict == "MATCH" else "BLOCKED"
    oid = ledger.create_obligation(
        vendor=vendor,
        contract_file=None,
        clause_text=clause_text,
        term_end=term_end,
        llm_deadline=proposed_deadline,
        engine_deadline=derivation.deadline.isoformat() if derivation.deadline else None,
        gate_verdict=verdict,
        status=status,
        notice_method="email",
        recipient=recipient,
        expected_final_amount=expected_final_amount,
    )
    ledger.append_receipt(oid, "EXTRACTED", {
        "vendor": vendor, "clause": clause_text, "term_end": term_end,
        "llm_deadline": proposed_deadline,
    })
    ledger.append_receipt(oid, "GATED", {
        "verdict": verdict,
        "engine_deadline": str(derivation.deadline),
        "reasons": derivation.reasons,
        "trace": derivation.trace,
    })
    return {
        "obligation_id": oid,
        "gate_verdict": verdict,
        "status": status,
        "engine_deadline": str(derivation.deadline),
        "engine_reasons": derivation.reasons,
    }


def request_notice_approval(obligation_id: int, notice_summary: str) -> dict:
    """Ask the human to approve sending the cancellation notice.

    This is a long-running operation: the run pauses (durably) until the
    approval webhook delivers the decision — even across process restarts.

    Args:
        obligation_id: The obligation awaiting notice.
        notice_summary: One-paragraph summary of what will be sent and to whom.
    """
    ledger.set_status(obligation_id, "AWAITING_APPROVAL")
    ledger.append_receipt(obligation_id, "APPROVAL_REQUESTED", {
        "summary": notice_summary,
    })
    return {"status": "pending", "obligation_id": obligation_id}


def send_notice(obligation_id: int, notice_text: str) -> dict:
    """Deliver the approved cancellation notice and hash-stamp the receipt.

    Args:
        obligation_id: The approved obligation.
        notice_text: The full formal non-renewal notice to deliver.
    """
    ob = ledger.get_obligation(obligation_id)
    if ob is None:
        return {"error": f"no obligation {obligation_id}"}
    delivery = inbox.deliver_notice(ob["vendor"], notice_text, obligation_id)
    receipt = ledger.append_receipt(obligation_id, "NOTICE_SENT", delivery)
    ledger.set_status(obligation_id, "NOTICE_SENT")
    return {"delivered": True, "receipt_hash": receipt["hash"], **delivery}


def check_invoice(obligation_id: int) -> dict:
    """Read the vendor's next-cycle invoice and verify billing stopped.

    The verdict is computed deterministically: invoice amount vs the expected
    final amount recorded at scheduling time.
    """
    ob = ledger.get_obligation(obligation_id)
    if ob is None:
        return {"error": f"no obligation {obligation_id}"}
    invoice = inbox.read_next_invoice(ob["vendor"])
    if invoice is None:
        invoice = inbox.seed_next_invoice(ob["vendor"], ob["status"] == "NOTICE_SENT")
    verdict = "VERIFIED" if invoice["amount"] <= ob["expected_final_amount"] else "REFUTED"
    ledger.append_receipt(obligation_id, "INVOICE_CHECKED", invoice)
    ledger.append_receipt(obligation_id, verdict, {
        "billed": invoice["amount"], "expected": ob["expected_final_amount"],
    })
    ledger.set_status(obligation_id, verdict)
    return {"verdict": verdict, "billed": invoice["amount"],
            "expected": ob["expected_final_amount"], "memo": invoice["memo"]}


def open_dispute(obligation_id: int, dispute_text: str) -> dict:
    """File a billing dispute attaching our own timestamped delivery receipt.

    Args:
        obligation_id: The REFUTED obligation.
        dispute_text: The dispute letter body (evidence is attached automatically).
    """
    notice_receipts = [
        r for r in ledger.get_receipts(obligation_id) if r["kind"] == "NOTICE_SENT"
    ]
    if not notice_receipts:
        return {"error": "no NOTICE_SENT receipt on file; cannot evidence the dispute"}
    evidence = notice_receipts[-1]
    ob = ledger.get_obligation(obligation_id)
    body = (
        f"{dispute_text}\n\n--- ATTACHED EVIDENCE ---\n"
        f"Notice delivered: {evidence['ts']}\n"
        f"Receipt SHA-256: {evidence['hash']}\n"
        f"Delivery record: {evidence['payload']}\n"
    )
    delivery = inbox.deliver_notice(ob["vendor"], body, obligation_id)
    ledger.append_receipt(obligation_id, "DISPUTE_OPENED", {
        "evidence_hash": evidence["hash"], **delivery,
    })
    ledger.set_status(obligation_id, "DISPUTED")
    return {"disputed": True, "evidence_hash": evidence["hash"], **delivery}


def get_obligation_status(obligation_id: int) -> dict:
    """Fetch an obligation's current state and receipt count."""
    ob = ledger.get_obligation(obligation_id)
    if ob is None:
        return {"error": f"no obligation {obligation_id}"}
    intact, _ = ledger.verify_chain(obligation_id)
    return {**ob, "receipts": len(ledger.get_receipts(obligation_id)),
            "chain_intact": intact}
