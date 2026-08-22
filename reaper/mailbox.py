"""The Reaper-owned mailbox: inbound documents, outbound notices, zero scopes.

Reaper reads exactly one mailbox — its own. A dedicated Gmail account with an
App Password, spoken to over IMAP/SMTP: no OAuth grant, no read scope on the
user's account, nothing to revoke because nothing was asked. The user forwards
a contract in, or writes one filter they own and can delete.

The read path is staged so each stage can be audited:
  1. headers only (BODY.PEEK — the \\Seen flag is not even set)
  2. deterministic admission: watchlisted vendor domain, the owner's own
     forward, or a reply to a message Reaper itself sent. Plain code, no model.
  3. full body ONLY for admitted messages; SHA-256 over the raw bytes first
  4. keyword prefilter — no renewal language, no model call
  5. redaction, then the normal intake pipeline
Every stage writes its counts into chain 0, declined ids included (hashed), so
"opened 3 of 1,247" is a claim the ledger can back.
"""

import email
import email.policy
import hashlib
import imaplib
import os
import re
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import make_msgid, parsedate_to_datetime

from . import inbox as vendor_world
from . import ledger

IMAP_HOST = os.getenv("REAPER_IMAP_HOST", "imap.gmail.com")
SMTP_HOST = os.getenv("REAPER_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("REAPER_SMTP_PORT", "465"))
MAIL_USER = os.getenv("REAPER_MAIL_USER", "").strip()
MAIL_PASS = os.getenv("REAPER_MAIL_APP_PASSWORD", "").replace(" ", "").strip()
OWNER = os.getenv("REAPER_OWNER_EMAIL", "").lower().strip()

_PREFILTER = re.compile(
    r"auto[- ]?renew|automatically renew|renewal term|notice period|"
    r"shall (?:be )?renewed|non[- ]?renewal|unless .{0,40}notice", re.I)


def configured() -> bool:
    return bool(MAIL_USER and MAIL_PASS)


def _sha(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()


def watchlist_domains() -> set[str]:
    """Vendor domains the mailbox is watched FOR — nothing else is opened."""
    domains = {v.replace(" ", "").replace(".", "").lower() + ".test"
               for v in vendor_world.VENDORS}
    extra = os.getenv("REAPER_VENDOR_DOMAINS", "")
    domains.update(d.strip().lower() for d in extra.split(",") if d.strip())
    return domains


@dataclass
class Admission:
    admitted: bool
    reason: str
    kind: str = "none"        # contract | reply | none


def admit(headers: dict, watchlist: set[str], owner: str,
          our_message_ids: set[str]) -> Admission:
    """Deterministic admission. Three doors in, all of them narrow."""
    sender = (headers.get("from") or "").lower()
    refs = f"{headers.get('in-reply-to', '')} {headers.get('references', '')}"

    for mid in our_message_ids:
        if mid and mid in refs:
            return Admission(True, "reply to a notice Reaper itself sent", "reply")
    if owner and owner in sender:
        return Admission(True, "forwarded in by the enrolled owner", "contract")
    for domain in watchlist:
        if domain and f"@{domain}" in sender:
            return Admission(True, f"watchlisted vendor domain {domain}", "contract")
    return Admission(False, "sender not watched", "none")


def prefilter(text: str) -> bool:
    """No renewal language means no model call. Plain regex, explainable."""
    return bool(_PREFILTER.search(text or ""))


def extract_text(msg: EmailMessage) -> tuple[str, dict | None]:
    """Best text part plus the first document attachment, if any."""
    body = ""
    attachment = None
    for part in msg.walk():
        ctype = part.get_content_type()
        dispo = str(part.get("Content-Disposition") or "")
        if ctype == "text/plain" and "attachment" not in dispo and not body:
            body = part.get_content()
        elif attachment is None and (
                ctype == "application/pdf" or ctype.startswith("image/")):
            attachment = {
                "filename": part.get_filename() or "document",
                "content_type": ctype,
                "data": part.get_payload(decode=True) or b"",
            }
    if not body and msg.get_body(preferencelist=("html",)) is not None:
        html = msg.get_body(preferencelist=("html",)).get_content()
        body = re.sub(r"<[^>]+>", " ", html)
    return body.strip(), attachment


@dataclass
class ScanResult:
    seen: int = 0
    admitted: list[dict] = field(default_factory=list)
    declined: int = 0
    declined_id_hashes: list[str] = field(default_factory=list)


def _sent_message_ids() -> set[str]:
    raw = ledger.get_meta("sent_message_ids") or ""
    return {m for m in raw.split(",") if m}


def _remember_sent(message_id: str, obligation_id: int) -> None:
    ids = _sent_message_ids()
    ids.add(message_id)
    ledger.set_meta("sent_message_ids", ",".join(sorted(ids)[-50:]))
    ledger.set_meta(f"notice_msgid_{message_id}", str(obligation_id))


def scan_once() -> ScanResult:
    """One staged pass over the owned mailbox. Blocking; run in a thread."""
    result = ScanResult()
    if not configured():
        return result

    watch = watchlist_domains()
    ours = _sent_message_ids()

    with imaplib.IMAP4_SSL(IMAP_HOST) as conn:
        conn.login(MAIL_USER, MAIL_PASS)
        conn.select("INBOX")
        _, data = conn.search(None, "UNSEEN")
        uids = data[0].split() if data and data[0] else []
        result.seen = len(uids)

        for uid in uids:
            # Stage 1: headers only, flags untouched.
            _, hdr = conn.fetch(uid, "(BODY.PEEK[HEADER.FIELDS "
                                     "(FROM TO SUBJECT DATE MESSAGE-ID "
                                     "IN-REPLY-TO REFERENCES)])")
            raw_hdr = hdr[0][1] if hdr and hdr[0] else b""
            parsed = email.message_from_bytes(raw_hdr, policy=email.policy.default)
            headers = {k.lower(): str(v) for k, v in parsed.items()}
            msgid = headers.get("message-id", uid.decode())

            # Stage 2: deterministic admission.
            ruling = admit(headers, watch, OWNER, ours)
            if not ruling.admitted:
                result.declined += 1
                result.declined_id_hashes.append(_sha(msgid)[:16])
                continue

            # Stage 3: the body, only now — hash before any transformation.
            _, full = conn.fetch(uid, "(BODY.PEEK[])")
            raw = full[0][1] if full and full[0] else b""
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            body, attachment = extract_text(msg)

            item = {
                "kind": ruling.kind,
                "reason": ruling.reason,
                "message_id": msgid,
                "message_id_hash": _sha(msgid)[:16],
                "from": headers.get("from", ""),
                "subject": headers.get("subject", ""),
                "content_sha256": _sha(raw),
                "body": body,
                "attachment": attachment,
                "in_reply_to": headers.get("in-reply-to", ""),
            }

            # Stage 4: a forwarded document with no renewal language never
            # reaches a model (replies are evidence and skip the filter).
            if ruling.kind == "contract" and not (
                    prefilter(body) or attachment is not None):
                result.declined += 1
                result.declined_id_hashes.append(_sha(msgid)[:16])
                ledger.log_access("MESSAGE_DISCARDED", {
                    "message_id_hash": item["message_id_hash"],
                    "reason": "admitted but no renewal language; no model called",
                })
                continue

            conn.store(uid, "+FLAGS", "\\Seen")
            result.admitted.append(item)

    ledger.log_access("MAILBOX_SCANNED", {
        "unseen": result.seen,
        "opened": len(result.admitted),
        "declined": result.declined,
        "declined_id_hashes": result.declined_id_hashes[:40],
        "note": "headers read with BODY.PEEK; bodies fetched for admitted only",
    })
    for item in result.admitted:
        ledger.log_access("MESSAGE_OPENED", {
            "message_id_hash": item["message_id_hash"],
            "reason": item["reason"],
            "content_sha256": item["content_sha256"],
        })
    return result


def record_vendor_reply(item: dict) -> int | None:
    """A vendor answered the notice thread: third-party evidence, the best kind."""
    oid = None
    for mid in _sent_message_ids():
        if mid and mid in item.get("in_reply_to", ""):
            raw = ledger.get_meta(f"notice_msgid_{mid}")
            oid = int(raw) if raw else None
            break
    if oid is None:
        return None
    from .privacy import redact
    snippet = redact((item.get("body") or "")[:400]).text
    ledger.append_receipt(oid, "VENDOR_REPLY", {
        "from": item.get("from"),
        "subject": item.get("subject"),
        "content_sha256": item["content_sha256"],
        "snippet": snippet,
        "note": "third-party-originated record captured from the notice thread",
    })
    return oid


def send_email(recipient: str, subject: str, body: str,
               obligation_id: int) -> dict | None:
    """Send a real notice from the owned mailbox; the Message-ID is evidence."""
    if not configured():
        return None
    msg = EmailMessage()
    msg["From"] = MAIL_USER
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=MAIL_USER.split("@")[-1])
    msg["Reply-To"] = MAIL_USER
    msg.set_content(body)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.login(MAIL_USER, MAIL_PASS)
        refused = smtp.send_message(msg)
    _remember_sent(msg["Message-ID"], obligation_id)
    return {
        "delivered_to": recipient,
        "smtp_host": SMTP_HOST,
        "message_id": msg["Message-ID"],
        "refused": {k: str(v) for k, v in refused.items()},
    }
