"""Agent-facing tools. The agent orchestrates; every verdict that matters
(deadline gate, invoice verdict, chain hashes) is computed here in plain code."""

from datetime import date
from pathlib import Path

from . import delivery, inbox, ledger, precedent, privacy, speech, vision
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

    # IDEMPOTENT FILING: the same contract filed twice (a retried turn, a
    # double upload) must not create a twin obligation. An active filing for
    # this vendor and term is returned as-is — with a receipt saying so.
    for existing in ledger.list_obligations():
        if (existing.get("vendor", "").strip().lower() == vendor.strip().lower()
                and existing.get("term_end") == term_end
                and existing.get("status") in ("SCHEDULED", "BLOCKED",
                                               "AWAITING_APPROVAL")):
            ledger.append_receipt(existing["id"], "DUPLICATE_FILING_IGNORED", {
                "note": "the same contract was filed again; the original "
                        "filing stands and no twin obligation was created",
            })
            return {
                "obligation_id": existing["id"],
                "gate_verdict": existing.get("gate_verdict"),
                "status": existing.get("status"),
                "engine_deadline": existing.get("engine_deadline"),
                "duplicate": True,
                "note": "this contract is already on file; returning the "
                        "existing obligation",
            }

    verdict, derivation = gate(proposed_d, clause_text, term_end_d)

    # HOW the notice must travel is read off the clause deterministically —
    # never assumed. An email is not a compliant notice under a registered-post
    # clause, and the ledger must say so from the moment of filing.
    ruling = delivery.classify(clause_text)

    # Prior resolved obligations are recalled AFTER the gate has already ruled,
    # so history can never move a verdict — it is context for the report. If
    # the store is off, empty or unreachable, this records a miss and changes
    # nothing about the filing.
    prior = precedent.recall(
        vendor=vendor,
        clause_text=clause_text,
        method=ruling.method,
        engine_deadline=derivation.deadline.isoformat() if derivation.deadline else None,
    )

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
    ledger.append_receipt(oid, "PRECEDENT_CONSULTED", _precedent_payload(
        prior, vendor=vendor, clause_text=clause_text,
        engine_deadline=derivation.deadline, delivery_method=ruling.method,
    ))
    ledger.append_receipt(oid, "GATED", {
        "verdict": verdict,
        "engine_deadline": str(derivation.deadline),
        "reasons": derivation.reasons,
        "trace": derivation.trace,
        "delivery_method": ruling.method,
        "email_compliant": ruling.email_compliant,
        "delivery_evidence": ruling.evidence,
        "notice_period_value": derivation.period_value,
        "notice_period_unit": derivation.period_unit,
        "anchor": derivation.anchor,
        "precedent": prior.get("summary"),
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
    if prior["available"] and prior["matches"]:
        result["precedent"] = prior["summary"]
        if prior.get("warning"):
            result["precedent_warning"] = prior["warning"]
    elif not prior["available"] and prior.get("reason") != "disabled":
        result["precedent"] = (
            f"precedent memory unavailable ({prior.get('reason')}); "
            "no prior history was read for this clause"
        )
    return result


def _precedent_payload(prior: dict, *, vendor: str, clause_text: str,
                       engine_deadline, delivery_method) -> dict:
    return {
        "available": prior["available"],
        "reason": prior.get("reason"),
        "backend": prior.get("backend"),
        "embedding_model": prior.get("embedding_model"),
        "embedding_dim": prior.get("embedding_dim"),
        "rows_scanned": prior.get("rows_scanned"),
        "latency_ms": prior.get("latency_ms"),
        "query": {
            "vendor": vendor,
            "clause_chars": len(clause_text or ""),
            "engine_deadline": str(engine_deadline),
            "delivery_method": delivery_method,
        },
        "matches": prior.get("matches", []),
        "summary": prior.get("summary"),
        "warning": prior.get("warning"),
        "note": ("prior resolved obligations consulted before the gate verdict "
                 "was filed; advisory only — precedent never changes a verdict"),
    }


def request_notice_approval(obligation_id: int, notice_summary: str) -> dict:
    """Ask the human to approve sending the cancellation notice.

    This is a long-running operation: the run pauses (durably) until the
    approval webhook delivers the decision — even across process restarts.

    Args:
        obligation_id: The obligation awaiting notice.
        notice_summary: One-paragraph summary of what will be sent and to whom.
    """
    ob = ledger.get_obligation(obligation_id)
    if ob is not None and ob["status"] == "AWAITING_APPROVAL":
        # Already waiting on a human. Asking again would put a second request
        # on their phone and quietly retire the first one they were looking at.
        return {"status": "pending", "obligation_id": obligation_id,
                "note": "this obligation is already awaiting a human decision"}
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

    # HARD GUARD, not an instruction: no notice leaves without a human
    # signature on the record AND a run that is actually at the approval step.
    # A degraded or fallback model that skips request_notice_approval — or a
    # turn from another phase reaching for this tool — is refused mechanically.
    chain = ledger.get_receipts(obligation_id)
    approved = [r for r in chain if r["kind"] == "APPROVED"]
    if not approved or ob.get("status") != "AWAITING_APPROVAL":
        ledger.log_access("SEND_REFUSED", {
            "obligation_id": obligation_id,
            "status_seen": ob.get("status"),
            "chain_kinds": [r["kind"] for r in chain][-8:],
            "reason": ("no human approval on record" if not approved
                       else "obligation is not awaiting approval"),
        })
        return {"error": "refused: this obligation has no human approval on "
                         "record at the approval step. Call "
                         "request_notice_approval and wait for the human "
                         "decision before sending anything."}
    approval_hash = approved[-1]["hash"]

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
    record["approved_by_receipt"] = approval_hash
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

    prior = [r for r in ledger.get_receipts(obligation_id)
             if r["kind"] == "INVOICE_CHECKED"]
    if prior:
        # The verdict on this invoice is already on the record. Reading it
        # again cannot change arithmetic, and restating it only crowds the
        # chain with the same finding.
        import json as _json
        payload = _json.loads(prior[-1]["payload"]) if isinstance(
            prior[-1]["payload"], str) else (prior[-1]["payload"] or {})
        return {"already_checked": True, "obligation_id": obligation_id,
                "verdict": ob["status"], "billed": payload.get("billed"),
                "expected": payload.get("expected"),
                "receipt_hash": prior[-1]["hash"]}

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
    chain = ledger.get_receipts(obligation_id)
    already = [r for r in chain if r["kind"] == "DISPUTE_OPENED"]
    if already:
        # One wrong invoice, one dispute. Filing it again would put the same
        # claim on the record several times over, and a chain that repeats
        # itself is worth less as evidence than one that does not.
        return {"already_filed": True, "obligation_id": obligation_id,
                "receipt_hash": already[0]["hash"],
                "note": "this dispute is already on the record"}
    notice_receipts = [r for r in chain if r["kind"] == "NOTICE_SENT"]
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


def ensure_dispute_filed(obligation_id: int) -> dict | None:
    """Backstop: a REFUTED verdict must never be left unfiled.

    Filing the dispute is the agent's own next step, but an agent turn can end
    early — model quota, a restart, a plan cut short. The verdict itself is
    arithmetic, so its consequence is not a judgement call: if an obligation is
    REFUTED, has a delivery receipt, and carries no dispute yet, the dispute is
    filed here and the chain records that the backstop did it, not the model.

    Returns None when nothing needed doing.
    """
    ob = ledger.get_obligation(obligation_id)
    if ob is None or ob["status"] != "REFUTED":
        return None
    kinds = {r["kind"] for r in ledger.get_receipts(obligation_id)}
    if "DISPUTE_OPENED" in kinds or "NOTICE_SENT" not in kinds:
        return None
    result = open_dispute(
        obligation_id,
        "This charge was raised after a valid notice of non-renewal was served "
        "within the contractual window. The attached delivery receipt evidences "
        "that service. We dispute the charge in full and request its reversal.",
    )
    if not result.get("disputed"):
        return result
    ledger.append_receipt(obligation_id, "DISPUTE_BACKSTOP", {
        "note": "the agent's run ended before filing; a refuted verdict is "
                "arithmetic, so the dispute was filed deterministically",
        "evidence_hash": result.get("evidence_hash"),
    })
    return result


def get_obligation_status(obligation_id: int) -> dict:
    """Fetch an obligation's current state and receipt count."""
    ob = ledger.get_obligation(obligation_id)
    if ob is None:
        return {"error": f"no obligation {obligation_id}"}
    intact, _ = ledger.verify_chain(obligation_id)
    return {**ob, "receipts": len(ledger.get_receipts(obligation_id)),
            "chain_intact": intact}


def recall_precedent(obligation_id: int) -> dict:
    """Look up how clauses shaped like this one resolved before.

    Advisory only. Precedent is prior history, never a verdict: nothing this
    returns can change a gate ruling, a deadline, or a billing verdict. If the
    precedent store is disabled or unreachable, that is reported plainly and
    the obligation is unaffected.

    Args:
        obligation_id: The obligation whose clause should be matched.
    """
    ob = ledger.get_obligation(obligation_id)
    if ob is None:
        return {"error": f"no obligation {obligation_id}"}
    prior = precedent.recall(
        vendor=ob.get("vendor") or "",
        clause_text=ob.get("clause_text") or "",
        method=ob.get("notice_method"),
        engine_deadline=ob.get("engine_deadline"),
    )
    ledger.append_receipt(obligation_id, "PRECEDENT_CONSULTED", _precedent_payload(
        prior, vendor=ob.get("vendor") or "",
        clause_text=ob.get("clause_text") or "",
        engine_deadline=ob.get("engine_deadline"),
        delivery_method=ob.get("notice_method"),
    ))
    out = {
        "available": prior["available"],
        "matches": prior["matches"],
        "summary": prior["summary"],
        "rows_scanned": prior.get("rows_scanned"),
    }
    if prior.get("warning"):
        out["precedent_warning"] = prior["warning"]
    if not prior["available"]:
        out["note"] = (
            f"precedent memory could not be read ({prior.get('reason')}); "
            "this is a gap in context, not a finding about the contract"
        )
    return out


def record_vendor_call(obligation_id: int, audio_path: str,
                       who_called: str = "unknown") -> dict:
    """Enter a phone call into the evidence chain by transcribing the recording.

    Cancellations get confirmed on the phone and then denied on the invoice.
    Every other channel here leaves a document; a call leaves a memory, and a
    memory is exactly what a dispute discounts. The recording's SHA-256 is
    stored beside the transcript, so the pair can be checked later: the
    transcript re-derives from the file, and the file is provably the one read.

    Args:
        obligation_id: The obligation this call was about.
        audio_path: Path to the call recording (wav, mp3, m4a, ogg).
        who_called: "vendor" or "customer" - who placed the call, if known.
    """
    ob = ledger.get_obligation(obligation_id)
    if ob is None:
        return {"error": f"no obligation {obligation_id}"}

    path = Path(audio_path)
    if not path.exists():
        return {"error": f"no recording at {audio_path}"}
    data = path.read_bytes()

    mime = {
        ".wav": "audio/wav", ".mp3": "audio/mp3", ".m4a": "audio/mp4",
        ".ogg": "audio/ogg", ".flac": "audio/flac", ".aac": "audio/aac",
        ".mp4": "audio/mp4", ".webm": "audio/webm",
    }.get(path.suffix.lower(), "audio/wav")

    # Vendor names and contract vocabulary are what generic speech models
    # mangle, and a mangled vendor name is weaker evidence than a spelled one.
    heard = speech.transcribe_call(data, mime, vocabulary=[
        ob.get("vendor") or "", "non-renewal", "notice period",
        "auto-renewal", ob.get("recipient") or "",
    ])

    if not heard["ok"]:
        ledger.log_access("CALL_UNREADABLE", {
            "obligation_id": obligation_id,
            "audio_sha256": heard["sha256"],
            "error": heard["error"],
            "note": "the recording is on file; it could not be transcribed",
        })
        return {"transcribed": False, "audio_sha256": heard["sha256"],
                "error": heard["error"],
                "note": "the call is recorded as unreadable rather than dropped"}

    # The transcript passes the same model boundary as contract text: whatever
    # was said, identifiers do not belong in a ledger anyone may later read.
    red = privacy.redact(heard["text"], keep_emails=True)

    receipt = ledger.append_receipt(obligation_id, "CALL_TRANSCRIBED", {
        "audio_sha256": heard["sha256"],
        "bytes": len(data),
        "mime": mime,
        "who_called": who_called,
        "model": heard["model"],
        "transcript": red.text,
        "masked": red.total,
        "audible": heard["audible"],
        "inaudible_marks": heard["inaudible"],
        "note": "verbatim transcript; the model was asked to transcribe and "
                "nothing else - what was said is a fact, what it means is not "
                "the transcriber's call",
    })
    return {
        "transcribed": True,
        "obligation_id": obligation_id,
        "audio_sha256": heard["sha256"],
        "receipt_hash": receipt["hash"],
        "model": heard["model"],
        "masked": red.total,
        "transcript": red.text,
    }
