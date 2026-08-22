import sys
from email.message import EmailMessage
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.mailbox import Admission, admit, extract_text, prefilter

WATCH = {"datavaultpro.test", "cloudcometrics.test"}
OWNER = "divij@example.com"
OURS = {"<reaper-notice-1@gmail.com>"}


def test_watchlisted_vendor_is_admitted():
    a = admit({"from": "Billing <accounts@datavaultpro.test>"}, WATCH, OWNER, OURS)
    assert a.admitted and a.kind == "contract"


def test_owner_forward_is_admitted():
    a = admit({"from": "Divij <divij@example.com>"}, WATCH, OWNER, OURS)
    assert a.admitted and a.kind == "contract"


def test_reply_to_our_notice_is_admitted_as_evidence():
    a = admit({"from": "anyone@anywhere.example",
               "in-reply-to": "<reaper-notice-1@gmail.com>"}, WATCH, OWNER, OURS)
    assert a.admitted and a.kind == "reply"


def test_stranger_is_declined():
    a = admit({"from": "newsletter@bigbank.example"}, WATCH, OWNER, OURS)
    assert not a.admitted
    assert a.kind == "none"


def test_bank_statement_from_unwatched_sender_stays_unopened():
    # The privacy beat: sensitive mail is never admitted because admission is
    # by sender, decided on headers alone.
    a = admit({"from": "statements@icicibank.example",
               "subject": "Your account statement"}, WATCH, OWNER, OURS)
    assert not a.admitted


def test_prefilter_requires_renewal_language():
    assert prefilter("This Agreement shall automatically renew for one year")
    assert prefilter("written notice of non-renewal at least sixty days")
    assert not prefilter("Team lunch is moved to Friday, please RSVP")


def test_extract_text_prefers_plain_and_finds_attachment():
    msg = EmailMessage()
    msg["From"] = "a@b.test"
    msg.set_content("Renewal terms attached.")
    msg.add_attachment(b"%PDF-1.4 fake", maintype="application",
                       subtype="pdf", filename="contract.pdf")
    body, att = extract_text(msg)
    assert "Renewal terms attached." in body
    assert att is not None
    assert att["filename"] == "contract.pdf"
    assert att["content_type"] == "application/pdf"
