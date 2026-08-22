"""Agent-facing tools. The agent orchestrates; every verdict that matters
(deadline gate, invoice verdict, chain hashes) is computed here in plain code."""

from datetime import date

from . import delivery, inbox, ledger, vision
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

    # HOW the notice must travel is read off the clause deterministically —
    # never assumed. An email is not a compliant notice under a registered-post
    # clause, and the ledger must say so from the moment of filing.
    ruling = delivery.classify(clause_text)

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
        notice_method=ruling.method,
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
        "delivery_method": ruling.method,
        "email_compliant": ruling.email_compliant,
        "delivery_evidence": ruling.evidence,
    })
    result = {
        "obligation_id": oid,
        "gate_verdict": verdict,
        "status": status,
        "engine_deadline": str(derivation.deadline),
        "engine_reasons": derivation.reasons,
        "delivery_method": ruling.method,
        "email_compliant": ruling.email_compliant,
    }
    if not ruling.email_compliant:
        result["delivery_warning"] = (
            f"the clause requires {ruling.method.replace('_', ' ').lower()} "
            f"(\"{ruling.evidence}\"); an email alone is NOT a compliant notice "
            "for this contract"
        )
    return result


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

    ruling = delivery.classify(ob.get("clause_text") or "")

    # A real mailbox sends a real email whose Message-ID becomes evidence and
    # whose thread captures the vendor's reply. Without one, the simulated
    # vendor world stands in — and the record says which happened.
    from . import mailbox
    record = None
    if mailbox.configured() and ob.get("recipient"):
        try:
            sent = mailbox.send_email(
                ob["recipient"],
                f"Notice of non-renewal — {ob['vendor']}",
                notice_text, obligation_id)
            if sent:
                record = {**sent, "channel": "smtp",
                          "delivered_on": date.today().isoformat()}
        except Exception as exc:
            record = None
            ledger.log_access("SEND_FAILED", {
                "obligation_id": obligation_id,
                "error": f"{type(exc).__name__}"[:80],
            })
    if record is None:
        record = inbox.deliver_notice(ob["vendor"], notice_text, obligation_id)
        record["channel"] = "simulated"
    record["delivery_method_required"] = ruling.method
    record["email_compliant"] = ruling.email_compliant
    if not ruling.email_compliant:
        # Honest labelling: this email is a courtesy copy, not the compliant
        # notice. The compliant channel still needs a human to act.
        record["compliance"] = "COURTESY_COPY_ONLY"
        record["compliant_channel_still_required"] = ruling.method

    receipt = ledger.append_receipt(obligation_id, "NOTICE_SENT", record)
    ledger.set_status(obligation_id, "NOTICE_SENT")
    out = {"delivered": True, "receipt_hash": receipt["hash"], **record}
    if not ruling.email_compliant:
        out["warning"] = (
            f"clause demands {ruling.method.replace('_', ' ').lower()}; the "
            "email is recorded as a courtesy copy and the printable notice pack "
            "must be dispatched by the compliant channel"
        )
    return out


def check_invoice(obligation_id: int) -> dict:
    """Read the vendor's next-cycle invoice document and verify billing stopped.

    The invoice arrives the way invoices actually arrive - as a document - so
    the agent reads it with Gemini vision. The model reports the figure printed
    on the page; the verdict itself is arithmetic, not judgement: billed against
    the amount recorded when the obligation was scheduled.
    """
    ob = ledger.get_obligation(obligation_id)
    if ob is None:
        return {"error": f"no obligation {obligation_id}"}

    if inbox.read_next_invoice(ob["vendor"]) is None:
        inbox.seed_next_invoice(ob["vendor"], ob["status"] == "NOTICE_SENT",
                                ob.get("term_end"))

    doc = inbox.invoice_path(ob["vendor"])
    seen = (vision.read_invoice(doc.read_bytes(), "image/jpeg")
            if doc.exists() else {"ok": False, "legible": False})

    if seen.get("ok") and seen.get("legible"):
        billed = float(seen.get("total_due") or 0.0)
        read_by = seen.get("read_by", "vision")
        currency = seen.get("currency", "")
        description = seen.get("description", "")
    else:
        issued = inbox.read_next_invoice(ob["vendor"]) or {}
        billed = float(issued.get("amount") or 0.0)
        read_by = "issuer record (document unreadable)"
        currency = issued.get("currency", "")
        description = issued.get("memo", "")

    expected = float(ob.get("expected_final_amount") or 0.0)
    verdict = "VERIFIED" if billed <= expected else "REFUTED"

    ledger.append_receipt(obligation_id, "INVOICE_CHECKED", {
        "document": doc.name, "read_by": read_by, "billed": billed,
        "currency": currency, "description": description,
        "invoice_number": seen.get("invoice_number"),
        "invoice_date": seen.get("invoice_date"),
    })
    ledger.append_receipt(obligation_id, verdict, {
        "billed": billed, "expected": expected,
        "test": "billed <= expected", "read_by": read_by,
    })
    ledger.set_status(obligation_id, verdict)
    return {"verdict": verdict, "billed": billed, "expected": expected,
            "currency": currency, "read_by": read_by, "memo": description}


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
