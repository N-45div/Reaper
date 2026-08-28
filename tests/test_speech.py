"""A phone call is evidence only if it can be checked afterwards.

What matters is not that the model transcribed something - it is that the
recording and the transcript are tied together by a hash, that identifiers are
masked before anything is stored, and that a call which could NOT be read is
recorded as unreadable instead of quietly disappearing.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reaper.ledger_sqlite as ledger_sqlite
from reaper import speech, tools


def _fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_sqlite, "LEDGER_PATH", tmp_path / "ledger.db")
    monkeypatch.setattr(tools, "ledger", ledger_sqlite)


def _obligation():
    return ledger_sqlite.create_obligation(
        vendor="DataVault Pro", contract_file=None,
        clause_text="renews unless ninety (90) days notice",
        term_end="2027-02-28", llm_deadline="2026-11-30",
        engine_deadline="2026-11-30", gate_verdict="MATCH",
        status="NOTICE_SENT", notice_method="email",
        recipient="accounts@datavaultpro.test", expected_final_amount=0.0,
    )


def _wav(tmp_path, name="call.wav", body=b"RIFFfake-audio-bytes"):
    p = tmp_path / name
    p.write_bytes(body)
    return p


def test_transcript_is_tied_to_the_recording_by_hash(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = _obligation()
    audio = _wav(tmp_path)
    monkeypatch.setattr(speech, "transcribe_call", lambda *a, **k: {
        "text": "SPEAKER 1: That subscription is cancelled, you won't be billed.",
        "ok": True, "audible": True, "model": "gemini-3.5-transcribe",
        "sha256": speech.audio_digest(audio.read_bytes()),
        "inaudible": False, "error": None})

    out = tools.record_vendor_call(obligation_id=oid, audio_path=str(audio),
                                   who_called="vendor")

    assert out["transcribed"] is True
    # the digest in the receipt must be the digest of the file on disk
    assert out["audio_sha256"] == speech.audio_digest(audio.read_bytes())
    payload = ledger_sqlite.get_receipts(oid)[-1]
    assert payload["kind"] == "CALL_TRANSCRIBED"
    assert out["audio_sha256"] in payload["payload"]
    assert "cancelled" in payload["payload"]


def test_identifiers_are_masked_before_the_transcript_is_stored(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = _obligation()
    audio = _wav(tmp_path)
    monkeypatch.setattr(speech, "transcribe_call", lambda *a, **k: {
        "text": "SPEAKER 2: card 4111 1111 1111 1111, call me on +91 98765 43210.",
        "ok": True, "audible": True, "model": "gemini-3.5-transcribe",
        "sha256": "abc", "inaudible": False, "error": None})

    out = tools.record_vendor_call(obligation_id=oid, audio_path=str(audio))

    assert "4111" not in out["transcript"]
    assert "98765" not in out["transcript"]
    assert out["masked"] >= 1
    assert "4111" not in ledger_sqlite.get_receipts(oid)[-1]["payload"]


def test_an_unreadable_call_is_recorded_not_dropped(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = _obligation()
    audio = _wav(tmp_path)
    monkeypatch.setattr(speech, "transcribe_call", lambda *a, **k: {
        "text": "", "ok": False, "audible": False,
        "model": "gemini-3.5-transcribe", "sha256": "deadbeef",
        "inaudible": False, "error": "ServerError: 503"})

    out = tools.record_vendor_call(obligation_id=oid, audio_path=str(audio))

    assert out["transcribed"] is False
    assert out["audio_sha256"] == "deadbeef"
    # no CALL_TRANSCRIBED receipt may claim a transcript that does not exist
    assert not [r for r in ledger_sqlite.get_receipts(oid)
                if r["kind"] == "CALL_TRANSCRIBED"]


def test_a_missing_recording_is_refused_plainly(tmp_path, monkeypatch):
    _fresh(tmp_path, monkeypatch)
    oid = _obligation()
    out = tools.record_vendor_call(obligation_id=oid,
                                   audio_path=str(tmp_path / "nope.wav"))
    assert "error" in out and "no recording" in out["error"]


def test_transcriber_never_raises(monkeypatch):
    """A failing model must return a fact, not an exception."""
    def boom(*a, **k):
        raise RuntimeError("network gone")
    monkeypatch.setattr(speech.llm, "call", boom)

    out = speech.transcribe_call(b"audio", "audio/wav")

    assert out["ok"] is False
    assert out["sha256"] == speech.audio_digest(b"audio")
    assert "RuntimeError" in out["error"]
