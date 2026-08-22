import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.delivery import classify
from reaper.privacy import redact


def test_email_address_in_clause_means_email_is_compliant():
    r = classify("written notice of non-renewal delivered to cancellations@cloudco.test")
    assert r.method == "EMAIL"
    assert r.email_compliant


def test_registered_post_blocks_email_compliance():
    r = classify("notice must be sent by registered post to the registered office")
    assert r.method == "REGISTERED_POST"
    assert not r.email_compliant


def test_alternatives_make_email_compliant_but_keep_strict_method():
    r = classify("notice may be given by email to legal@v.test or by certified mail")
    assert r.method == "CERTIFIED_MAIL"
    assert r.email_compliant


def test_courier_only():
    r = classify("delivered by a reputable courier service to the address above")
    assert r.method == "COURIER"
    assert not r.email_compliant


def test_silent_clause_defaults_to_unspecified_and_email_ok():
    r = classify("either party may terminate with sixty (60) days written notice")
    assert r.method == "UNSPECIFIED"
    assert r.email_compliant


def test_contract_profile_keeps_the_notice_address():
    # The document profile must not mask the address the notice depends on.
    clause = "notice to cancellations@cloudcometrics.test, card 4111 1111 1111 1111"
    r = redact(clause, keep_emails=True)
    assert "cancellations@cloudcometrics.test" in r.text
    assert "[redacted:1111]" in r.text
