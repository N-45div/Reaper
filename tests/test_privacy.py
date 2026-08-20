import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reaper.privacy import redact


def test_card_number_is_masked_but_keeps_last_four():
    r = redact("Charged to card 4111 1111 1111 1111 on Tuesday.")
    assert "4111 1111 1111 1111" not in r.text
    assert "[redacted:1111]" in r.text
    assert r.counts["card number"] == 1


def test_number_that_fails_luhn_is_left_alone():
    # Not a real card: must not be masked just for being long.
    r = redact("Reference 1234567890123456 applies.")
    assert "1234567890123456" in r.text
    assert "card number" not in r.counts


def test_pan_and_gstin_are_masked():
    r = redact("PAN ABCDE1234F and GSTIN 27ABCDE1234F1Z5 on file.")
    assert "ABCDE1234F" not in r.text
    assert "27ABCDE1234F1Z5" not in r.text
    assert r.counts["PAN"] == 1


def test_aadhaar_with_valid_checksum_is_masked():
    r = redact("Aadhaar 2345 6789 0124 attached.")           # valid Verhoeff
    assert "2345 6789 0124" not in r.text
    assert r.counts["Aadhaar number"] == 1


def test_email_keeps_the_domain_but_drops_the_person():
    r = redact("Write to priya.sharma@datavaultpro.test for billing.")
    assert "priya.sharma" not in r.text
    assert "@datavaultpro.test" in r.text


def test_contract_substance_survives_untouched():
    clause = ("This Agreement renews for successive twelve (12) month terms unless "
              "written notice is given at least sixty (60) days prior to the end of "
              "the then-current term, which ends 2026-12-31. Fee: $540.00 per month.")
    r = redact(clause)
    assert "sixty (60) days" in r.text
    assert "twelve (12) month" in r.text
    assert "2026-12-31" in r.text
    assert "$540.00" in r.text


def test_summary_reports_what_was_masked_without_the_values():
    r = redact("card 4111 1111 1111 1111, PAN ABCDE1234F")
    assert "card number" in r.summary()
    assert "4111" not in r.summary().replace("[redacted:1111]", "")
    assert r.total == 2


def test_empty_input_is_safe():
    r = redact("")
    assert r.text == ""
    assert r.total == 0
