"""The approval channel: the one human moment, delivered to a phone.

Telegram, deliberately. getUpdates long-polling PULLS the decision outward
from this machine, so the approval path opens no inbound port, needs no public
webhook, no tunnel and no certificate — on a project with no Cloud Run that is
not a convenience but the design.

A tap on APPROVE here sends a contractual notice, so the button is not a
button, it is an authorization:
  - each request carries a single-use random token bound to one obligation;
  - only the enrolled chat id may answer; anyone else is logged and ignored;
  - the message carries NO contract text — an id, a vendor, a date, a hash
    prefix. The draft stays behind the app.

Not configured (no token/chat id in the environment) means cleanly absent:
the in-app approval keeps working exactly as before.
"""

import asyncio
import os
import secrets

import aiohttp

from . import ledger

TOKEN = os.getenv("REAPER_TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("REAPER_TELEGRAM_CHAT_ID", "").strip()


def configured() -> bool:
    return bool(TOKEN and CHAT_ID)


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{TOKEN}/{method}"


async def notify_pending(obligation: dict, receipt_hash: str | None = None) -> bool:
    """Offer the approval on the phone. Returns True if the message went out."""
    if not configured():
        return False
    oid = obligation["id"]
    # One open pause, one token. Minting a fresh one for every offer silently
    # expires the message already on the owner's phone - so the tap they make
    # is refused for a reason that is true but useless to them.
    token = ledger.get_meta(f"approval_token_{oid}") or secrets.token_urlsafe(9)
    ledger.set_meta(f"approval_token_{oid}", token)

    deadline = obligation.get("engine_deadline") or "?"
    hash_note = f"\nchain {receipt_hash[:12]}…" if receipt_hash else ""
    text = (f"Reaper — obligation №{oid:04d}\n"
            f"{obligation.get('vendor', '?')}: non-renewal notice drafted.\n"
            f"Notice deadline {deadline}.{hash_note}\n\n"
            "Approve sending it?")
    # One button per row: side-by-side labels clip on narrow phones, and the
    # thing being tapped here deserves to be legible in full.
    keyboard = {"inline_keyboard": [
        [{"text": "✓ Sign — send the notice", "callback_data": f"d:{oid}:{token}:y"}],
        [{"text": "✗ Decline", "callback_data": f"d:{oid}:{token}:n"}],
    ]}
    await supersede(oid)   # only one request may be live on the phone at a time
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(_api("sendMessage"), json={
                "chat_id": CHAT_ID, "text": text, "reply_markup": keyboard,
            }, timeout=aiohttp.ClientTimeout(total=20)) as r:
                body = await r.json()
                ok = body.get("ok", False)
                if ok:
                    # Remember the message so it can be retired later: an old
                    # request left tappable is a trap - the tap is refused for
                    # a reason that is true and tells the human nothing.
                    mid = (body.get("result") or {}).get("message_id")
                    if mid:
                        ledger.set_meta(f"approval_msg_{oid}", str(mid))
        if ok:
            ledger.append_receipt(oid, "APPROVAL_OFFERED", {
                "channel": "telegram", "chat_id": CHAT_ID,
                "note": "no contract text left the app; id, vendor, date and "
                        "hash prefix only",
            })
        return ok
    except Exception as exc:
        # The in-app path still stands; never block the run — but a channel
        # failure is a fact worth recording, not hiding.
        try:
            ledger.log_access("PHONE_NOTIFY_FAILED", {
                "obligation_id": oid, "error": f"{type(exc).__name__}: {exc}"[:160],
            })
        except Exception:
            pass
        return False


async def supersede(oid: int, note: str = "This request is no longer active.") -> None:
    """Retire the request currently on the phone for this obligation.

    A message with buttons stays tappable forever. Once its decision has been
    taken - or the world it belonged to is gone - tapping it can only produce a
    refusal, which reads as a broken product rather than a stale message. So
    the message says so itself.
    """
    if not configured():
        return
    mid = ledger.get_meta(f"approval_msg_{oid}")
    if not mid:
        return
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(_api("editMessageText"), json={
                "chat_id": CHAT_ID, "message_id": int(mid),
                "text": "Reaper - obligation No.%04d\n%s" % (oid, note),
            }, timeout=aiohttp.ClientTimeout(total=15))
    except Exception:
        pass  # a message that cannot be edited is not worth failing a run over
    ledger.set_meta(f"approval_msg_{oid}", "")


def _parse(data: str) -> tuple[int, str, bool] | None:
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "d":
        return None
    try:
        return int(parts[1]), parts[2], parts[3] == "y"
    except ValueError:
        return None


async def _answer(session: aiohttp.ClientSession, callback_id: str, text: str) -> None:
    await session.post(_api("answerCallbackQuery"),
                       json={"callback_query_id": callback_id, "text": text},
                       timeout=aiohttp.ClientTimeout(total=15))


async def _handle_callback(session: aiohttp.ClientSession, cq: dict, decide) -> None:
    callback_id = cq.get("id", "")
    from_chat = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
    parsed = _parse(cq.get("data", ""))

    if parsed is None:
        await _answer(session, callback_id, "Not understood.")
        return
    oid, token, approve = parsed

    # An unknown chat pressing the button is a security event, not an approval.
    if from_chat != CHAT_ID:
        ledger.log_access("APPROVAL_DENIED", {
            "channel": "telegram", "reason": "unenrolled chat",
            "chat_id": from_chat, "obligation_id": oid,
        })
        await _answer(session, callback_id, "This device is not enrolled.")
        return

    expected = ledger.get_meta(f"approval_token_{oid}")
    if not expected or token != expected:
        ledger.log_access("APPROVAL_DENIED", {
            "channel": "telegram", "reason": "stale or invalid token",
            "obligation_id": oid,
        })
        await _answer(session, callback_id, "This approval request has expired.")
        return

    ledger.set_meta(f"approval_token_{oid}", "")  # single use
    await _answer(session, callback_id, "Delivering your decision…")
    result = await decide(oid, approve, {"channel": "telegram", "chat_id": from_chat})
    verdict = ("nothing was pending" if result is None
               else "notice sent ✓" if approve else "declined — nothing sent")
    await supersede(oid, f"Signed. {verdict}")
    await session.post(_api("sendMessage"), json={
        "chat_id": CHAT_ID,
        "text": f"Obligation №{oid:04d}: {verdict}",
    }, timeout=aiohttp.ClientTimeout(total=15))


async def poll_loop(decide) -> None:
    """Long-poll for decisions. `decide(oid, approve, via)` delivers one."""
    offset = 0
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                while True:
                    async with session.get(_api("getUpdates"), params={
                        "timeout": 50, "offset": offset,
                        "allowed_updates": '["callback_query"]',
                    }, timeout=aiohttp.ClientTimeout(total=70)) as r:
                        data = await r.json()
                    for update in data.get("result", []):
                        offset = update["update_id"] + 1
                        cq = update.get("callback_query")
                        if cq:
                            await _handle_callback(session, cq, decide)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(10)  # network blip; resume polling
