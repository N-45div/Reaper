"""Reaper service: contract intake, notice-window wakes, approval webhook, evidence API.

The approval flow is the demo centerpiece: request_notice_approval pauses the
run durably (DatabaseSessionService); the pointer rows in the ledger let a
freshly restarted process resume the exact paused invocation.
"""

import asyncio
import time
import io
import os
import uuid
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from pydantic import BaseModel

from google.adk.agents.run_config import RunConfig

from reaper import approvals, clock, inbox, ledger, llm, mailbox, privacy, tools, vision
from reaper.agent import app as adk_app
from reaper.config import APP_NAME, DB_URL
from reaper.triage import triage_contract

STATIC_DIR = Path(__file__).parent / "reaper" / "static"

USER_ID = "owner"
WAKE_LEAD_DAYS = int(os.getenv("REAPER_WAKE_LEAD_DAYS", "7"))
TICK_SECONDS = int(os.getenv("REAPER_TICK_SECONDS", "6"))
runner: Runner | None = None
_in_flight: set[int] = set()
_detached: set[asyncio.Task] = set()


def _reap(task: asyncio.Task) -> None:
    """Retire a detached run and make sure a failure is never silent."""
    _detached.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        try:
            ledger.log_access("RUN_INCOMPLETE", {
                "error": type(exc).__name__,
                "note": "detached run failed; the ledger holds the true state",
            })
        except Exception:
            pass


# An agent turn runs model calls and ledger writes through synchronous
# clients, so it holds the event loop in stretches. Waiting on it inside the
# request is therefore doubly wrong: the hosted proxy gives up after about a
# minute, and an async timeout cannot fire reliably on a busy loop. The turn is
# handed to a background task and the caller is answered immediately.
SYNC_WAIT_SECONDS = float(os.getenv("REAPER_SYNC_WAIT_SECONDS", "0"))


async def _detached_run(coro, *, obligation_id: int | None = None,
                        wait: float | None = None) -> dict | None:
    """Own an agent turn in a task that outlives the HTTP request.

    Losing the connection must never lose the work, so the turn is a background
    task and the ledger is how you learn the outcome. Set
    REAPER_SYNC_WAIT_SECONDS to block for the full report instead — convenient
    on a laptop, unsafe behind a proxy.
    """
    task = asyncio.create_task(coro)
    _detached.add(task)
    task.add_done_callback(_reap)
    wait = SYNC_WAIT_SECONDS if wait is None else wait
    if wait > 0:
        done, _pending = await asyncio.wait({task}, timeout=wait)
        if task in done:
            return task.result()
    return {
        "accepted": True,
        "still_running": True,
        "note": "the agent is working; poll /obligations or "
                f"/obligations/{obligation_id}/receipts for the outcome",
    }


async def _ticker():
    """The agent's own heartbeat: watch the ledger calendar and act unprompted.

    In production this is a Cloud Scheduler -> Pub/Sub wake; locally the same
    logic runs on an in-process loop so the autonomy is visible in the demo.
    """
    wake_backoff: dict[int, tuple[float, int]] = {}  # oid -> (not_before, fails)
    # Boot grace: waking an overdue obligation in the first seconds of a new
    # process competes with startup for the event loop, and a health check
    # that cannot be answered reads as a dead instance.
    await asyncio.sleep(max(TICK_SECONDS, 45))
    while True:
        await asyncio.sleep(TICK_SECONDS)
        # A heartbeat that can die is not a heartbeat. One transient
        # failure here - a ledger blip, a bad row - used to end this
        # loop for the life of the process: no wake, no receipt, no
        # error, just an obligation that sits at SCHEDULED forever.
        try:
            today = clock.today()
            # Ledger I/O is synchronous gRPC. On the event loop it starves the
            # health check, and a health check that cannot be answered gets the
            # instance restarted - killing the very run it was waiting for.
            for ob in await asyncio.to_thread(ledger.list_obligations):
                oid = ob["id"]
                if oid in _in_flight:
                    continue
                nb, fails = wake_backoff.get(oid, (0.0, 0))
                if time.monotonic() < nb:
                    continue  # a recent wake failed; don't burn quota every tick
                try:
                    if ob["status"] == "SCHEDULED" and ob["engine_deadline"]:
                        deadline = date.fromisoformat(ob["engine_deadline"])
                        if today >= deadline - timedelta(days=WAKE_LEAD_DAYS):
                            _in_flight.add(oid)
                            await asyncio.to_thread(
                                ledger.append_receipt, oid, "WOKE", {
                                    "reason": "calendar entered the notice window",
                                    "ledger_date": today.isoformat(),
                                    "deadline": ob["engine_deadline"],
                                })
                            task = asyncio.create_task(_open_notice_window(oid))
                            _detached.add(task)
                            task.add_done_callback(_reap)
                            try:
                                r = await task
                            except asyncio.CancelledError:
                                if task.cancelled():
                                    continue  # a reset drew the world boundary
                                raise
                            # A failed model call can end the stream SILENTLY -
                            # no exception, no degraded flag. The ledger is the
                            # truth: a wake that left the obligation SCHEDULED
                            # did not work, whatever the run reported.
                            _docket_changed()
                            after = await asyncio.to_thread(ledger.get_obligation, oid)
                            stuck = after is not None and after["status"] == "SCHEDULED"
                            if r.get("degraded") or stuck:
                                ledger.log_access("WAKE_DEGRADED", {
                                    "obligation_id": oid, "fails": fails + 1,
                                    "reason": str(r.get("degraded") or "run ended without effect")[:160],
                                })
                                wake_backoff[oid] = (time.monotonic() + min(600, 60 * (2 ** fails)), fails + 1)
                            else:
                                wake_backoff.pop(oid, None)
                    elif ob["status"] == "AWAITING_APPROVAL":
                        # A decision already given, whose work never finished. The
                        # human signed; then the process died before the resumed
                        # run could deliver. Nothing else would ever pick this up,
                        # so the signature would sit on the chain having achieved
                        # nothing - the one outcome a durable pause exists to
                        # prevent. Finish what the human already authorised.
                        kinds = {r["kind"] for r in
                                 await asyncio.to_thread(ledger.get_receipts, oid)}
                        decided = kinds & {"APPROVED", "REJECTED"}
                        if decided and "NOTICE_SENT" not in kinds:
                            ptr = await asyncio.to_thread(
                                ledger.get_resume_pointer, oid)
                            if ptr is not None:
                                _in_flight.add(oid)
                                await asyncio.to_thread(
                                    ledger.log_access, "DECISION_RECOVERED", {
                                        "obligation_id": oid,
                                        "decision": sorted(decided)[0],
                                        "note": "the run carrying this decision "
                                                "did not finish; resuming it",
                                    })
                                task = asyncio.create_task(_deliver_decision(
                                    oid, "APPROVED" in decided,
                                    via={"channel": "recovered"}))
                                _detached.add(task)
                                task.add_done_callback(_reap)
                                try:
                                    await task
                                except asyncio.CancelledError:
                                    if task.cancelled():
                                        continue
                                    raise
                    elif ob["status"] == "NOTICE_SENT":
                        term_end = date.fromisoformat(ob["term_end"])
                        # The next-cycle invoice is checked once. Without this
                        # the wake refires every tick while the verdict settles,
                        # and the register fills with the same reading over and
                        # over - true records, but a chain that repeats itself
                        # reads as noise rather than evidence.
                        seen_invoice = any(
                            r["kind"] == "INVOICE_CHECKED"
                            for r in await asyncio.to_thread(
                                ledger.get_receipts, oid))
                        if today > term_end and not seen_invoice:
                            _in_flight.add(oid)
                            await asyncio.to_thread(
                                ledger.append_receipt, oid, "WOKE", {
                                    "reason": "term ended; next-cycle invoice due",
                                    "ledger_date": today.isoformat(),
                                })
                            task = asyncio.create_task(_process_invoice(oid))
                            _detached.add(task)
                            task.add_done_callback(_reap)
                            try:
                                await task
                            except asyncio.CancelledError:
                                if task.cancelled():
                                    continue  # a reset drew the world boundary
                                raise
                except Exception as exc:
                    # Transient (e.g. model quota) — retried on a later tick, but
                    # the chain should say a wake happened and did not finish.
                    # Back off here too: without it a raising wake is retried on
                    # every single tick, which is how one failure becomes a storm.
                    wake_backoff[oid] = (time.monotonic() + min(600, 60 * (2 ** fails)),
                                         fails + 1)
                    try:
                        ledger.log_access("WAKE_INCOMPLETE", {
                            "obligation_id": oid,
                            "error": type(exc).__name__,
                            "note": "retried on a later tick",
                        })
                    except Exception:
                        pass
                finally:
                    _in_flight.discard(oid)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                await asyncio.to_thread(ledger.log_access, "HEARTBEAT_ERROR", {
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                    "note": "the tick failed; the heartbeat continues",
                })
            except Exception:
                pass


async def _mailbox_loop():
    """Poll the owned mailbox; feed admitted documents into the normal intake."""
    poll = int(os.getenv("REAPER_MAIL_POLL_SECONDS", "60"))
    while True:
        await asyncio.sleep(poll)
        try:
            result = await asyncio.to_thread(mailbox.scan_once)
            for item in result.admitted:
                if item["kind"] == "reply":
                    oid = mailbox.record_vendor_reply(item)
                    if oid is None:
                        ledger.log_access("REPLY_UNMATCHED", {
                            "message_id_hash": item["message_id_hash"]})
                    continue
                text = item["body"] or ""
                if item.get("attachment") and item["attachment"]["data"]:
                    att = item["attachment"]
                    att_text, _src = _read_contract(
                        att["filename"], att["data"], att["content_type"])
                    text = f"{text}\n\n{att_text}".strip()
                if text.strip():
                    await _intake(text, source={
                        "how": "email",
                        "detail": item["reason"],
                        "message_id_hash": item["message_id_hash"],
                        "content_sha256": item["content_sha256"],
                    })
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # transient (network, quota); next poll retries


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global runner
    session_service = DatabaseSessionService(db_url=DB_URL)
    runner = Runner(app=adk_app, session_service=session_service)
    tasks = [asyncio.create_task(_ticker())]
    if approvals.configured():
        tasks.append(asyncio.create_task(approvals.poll_loop(_deliver_decision)))
    if mailbox.configured():
        tasks.append(asyncio.create_task(_mailbox_loop()))
    yield
    for t in tasks:
        t.cancel()


api = FastAPI(title="Reaper", lifespan=lifespan)


# A turn of this agent needs a handful of model calls: read, gate, report. The
# framework's default ceiling is five hundred, which is not a safety net but a
# cannon - one confused turn can spend a day's free-tier allowance in seconds.
_RUN_CONFIG = RunConfig(max_llm_calls=int(os.getenv("REAPER_MAX_LLM_CALLS", "12")))
TURN_TIMEOUT_S = float(os.getenv("REAPER_TURN_TIMEOUT_S", "180"))


async def _run(session_id: str, *, text: str | None = None,
               parts: list | None = None, invocation_id: str | None = None) -> dict:
    """Run one turn; collect final text and any pending long-running approval."""
    try:
        await runner.session_service.create_session(
            app_name=APP_NAME, user_id=USER_ID, session_id=session_id
        )
    except Exception:
        pass  # session already exists (resume / follow-up turn)

    content = types.Content(
        role="user",
        parts=parts if parts is not None else [types.Part(text=text)],
    )

    _docket_changed()
    final_text, pending, degraded, used_session = None, None, None, session_id
    # Quota errors are retried HERE, inside the turn, stepping keys and then
    # the model ladder — not deferred to a later tick. A wake that waits for
    # the next heartbeat leaves a human-visible pause with nothing pending,
    # which is exactly the window a crash turned into a stranded approval.
    #
    # Fresh turns retry on a FRESH session: an invocation that died mid-flight
    # can leave the old session un-runnable, and retrying into it would fail
    # every attempt no matter how much quota the next key has. Resumes never
    # switch sessions — the pointer names the one durable pause.
    # One attempt per (key, model) bucket that still has quota - capped so a
    # genuinely dead world fails fast rather than grinding.
    tries = max(2, min(8, llm.buckets_available()))
    for attempt in range(tries):
        use_session = session_id if (invocation_id or attempt == 0)             else f"{session_id}.r{attempt}"
        if use_session != session_id:
            try:
                await runner.session_service.create_session(
                    app_name=APP_NAME, user_id=USER_ID, session_id=use_session
                )
            except Exception:
                pass
        kwargs = {"user_id": USER_ID, "session_id": use_session,
                  "new_message": content, "run_config": _RUN_CONFIG}
        if invocation_id:
            kwargs["invocation_id"] = invocation_id
        try:
            events = 0
            # A model call that never returns would otherwise hold this
            # obligation in flight forever - silently, since nothing raises.
            async with asyncio.timeout(TURN_TIMEOUT_S):
                async for event in runner.run_async(**kwargs):
                    events += 1
                    pending = _pending_approval(event) or pending
                    final_text = _final_text(event) or final_text
            if events == 0 or (pending is None and not final_text):
                # A turn that yields nothing at all is not success. It has
                # happened silently - no exception, no output - and without
                # this record there is nothing to debug but a stuck status.
                try:
                    ledger.log_access("RUN_EMPTY", {
                        "session": use_session,
                        "events": events,
                        "model": llm.current_model(),
                        "key_index": llm.current_bucket()[0],
                        "note": "the model produced no usable turn",
                    })
                except Exception:
                    pass
                if attempt < tries - 1:
                    llm.rotate()   # a model that says nothing is not the one
                    adk_app.root_agent.model.model = llm.current_model()
                    continue
            degraded = None
            used_session = use_session
            break
        except Exception as exc:  # model quota/transient errors mid-run
            # The failure is a fact worth keeping: which attempt, which error.
            try:
                ledger.log_access("RUN_ATTEMPT_FAILED", {
                    "session": use_session, "attempt": attempt,
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                })
            except Exception:
                pass
            if pending is not None:
                # The pause is the outcome, and it already exists. Re-running
                # the turn would ask a second time, mint a second approval
                # token, and invalidate the request already sitting on the
                # owner's phone. Keep what landed.
                degraded = None
                used_session = use_session
                break
            if (llm.is_quota_error(exc) or llm.is_transient(exc)) and attempt < tries - 1:
                if llm.is_quota_error(exc):
                    llm.mark_dry(exc)   # that pair is refusing; step over it
                # A model that is merely busy keeps its allowance - but this
                # turn still needs an answer, and a different model usually
                # has one. Rotating is cheaper than failing the wake.
                llm.rotate()        # move to one that can answer now
                adk_app.root_agent.model.model = llm.current_model()
                # Rotation means the next pair is a different bucket, so there
                # is nothing to wait out - unless every pair is spent, in which
                # case the API's own retry delay is the only honest guess.
                await asyncio.sleep(1.5 if llm.buckets_available() else
                                    min(65.0, (llm.retry_after(exc) or 20) + 5))
                continue
            # Tool effects already committed are in the ledger; the session is
            # in SQL. Nothing is lost — report the ledger truth, not a 500.
            degraded = (
                f"model call failed mid-run ({type(exc).__name__}); completed tool "
                "actions are persisted in the ledger and the session is resumable"
            )
            break
    return {"final_text": final_text, "pending": pending, "degraded": degraded,
            "session_id": used_session}


def _pending_approval(event) -> dict | None:
    """Detect a long-running approval call pausing this run."""
    lro_ids = getattr(event, "long_running_tool_ids", None) or set()
    if not (event.content and event.content.parts):
        return None
    for part in event.content.parts:
        fc = getattr(part, "function_call", None)
        if fc is not None and fc.id in lro_ids:
            return {
                "function_call_id": fc.id,
                "function_name": fc.name,
                "invocation_id": event.invocation_id,
            }
    return None


def _final_text(event) -> str | None:
    if not (event.is_final_response() and event.content and event.content.parts):
        return None
    texts = [p.text for p in event.content.parts if getattr(p, "text", None)]
    return "\n".join(texts) if texts else None


@api.get("/")
async def landing():
    return FileResponse(STATIC_DIR / "landing.html")


@api.get("/static/{name}")
async def static_asset(name: str):
    """Serve a front-end asset.

    A deployment may layer extra assets over the built-ins by pointing
    REAPER_STATIC_OVERLAY at a directory; overlay files win by name.
    """
    safe = Path(name).name
    overlay = os.getenv("REAPER_STATIC_OVERLAY", "")
    if overlay:
        candidate = Path(overlay) / safe
        if candidate.is_file():
            return FileResponse(candidate)
    path = STATIC_DIR / safe
    if not path.exists():
        raise HTTPException(404, "no such asset")
    return FileResponse(path)


@api.get("/app")
async def dashboard():
    return FileResponse(STATIC_DIR / "index.html")


@api.post("/demo/reset")
async def demo_reset():
    # A reset is a WORLD BOUNDARY. Any agent turn still in flight belongs to
    # the old world; letting it finish would let it write into the new one
    # (a resumed notice once landed on a freshly-filed obligation this way).
    cancelled = 0
    for task in list(_detached):
        if not task.done():
            task.cancel()
            cancelled += 1
    if cancelled:
        await asyncio.sleep(0.2)  # let cancellations land before the wipe
    # Any request still sitting on the owner's phone belongs to the world that
    # is about to be deleted. Retire it now, while its record still exists -
    # otherwise it stays tappable forever and the tap is met with a refusal
    # that looks like a broken product instead of a stale message.
    try:
        for ob in await asyncio.to_thread(ledger.list_obligations):
            if ob.get("status") == "AWAITING_APPROVAL":
                await approvals.supersede(
                    ob["id"], "This run has ended. A new request will follow.")
    except Exception:
        pass
    # The wipe runs off the event loop and the response only says ok once the
    # world verifiably reads empty — a take must never start on a
    # half-deleted stage.
    _docket_changed()
    result = await asyncio.to_thread(ledger.reset_all)
    await asyncio.to_thread(inbox.reset_world)
    return {"ok": True, "purged": result, "cancelled_runs": cancelled}


async def _intake(text: str, source: dict | None = None) -> dict:
    # THE MODEL BOUNDARY. Raw text stops here: identifiers are masked before
    # any model sees a byte. The contract profile keeps email addresses (the
    # notice depends on one) but cards, Aadhaar, PAN, GSTIN, IBAN, IFSC,
    # passports and phone numbers never leave the machine.
    red = await asyncio.to_thread(privacy.redact, text, keep_emails=True)

    # Gemma triage: skip the expensive agent run when there is no renewal
    # clause at all. Fails open — triage never blocks a real contract.
    # It is a blocking HTTP call, so it belongs in a worker thread: seconds
    # spent here on the event loop are seconds the health check goes unanswered.
    triage = await asyncio.to_thread(triage_contract, red.text)
    redaction = {"profile": "document", "masked": red.total,
                 "summary": red.summary()}
    if triage["ok"] and not triage["has_renewal"]:
        return {"session_id": None, "triage": triage, "degraded": None,
                "redaction": redaction,
                "report": "Triage: no auto-renewal clause found in this "
                          f"document ({triage['model']}). Nothing to schedule.",
                "obligations": await asyncio.to_thread(ledger.list_obligations)}

    before = {o["id"] for o in await asyncio.to_thread(ledger.list_obligations)}
    session_id = f"intake-{uuid.uuid4().hex[:8]}"
    result = await _run(
        session_id,
        text="INTAKE. Extract the auto-renewal obligation from this contract "
             f"and gate-schedule it.\n\n--- CONTRACT ---\n{red.text}",
    )
    # Provenance of the reading and of the masking both belong in the chain.
    for ob in await asyncio.to_thread(ledger.list_obligations):
        if ob["id"] not in before:
            if source:
                await asyncio.to_thread(
                    ledger.append_receipt, ob["id"], "READ_AS", source)
            await asyncio.to_thread(
                ledger.append_receipt, ob["id"], "REDACTED", {
                    "profile": "document", "masked": red.total,
                    "kinds": red.counts,
                    "note": "models received the masked text only",
                })
    return {"session_id": session_id, "report": result["final_text"],
            "triage": triage, "degraded": result["degraded"],
            "redaction": redaction,
            "obligations": await asyncio.to_thread(ledger.list_obligations)}


def _read_contract(filename: str, raw: bytes, content_type: str) -> tuple[str, dict]:
    """Get contract text from a file, however it arrived.

    Digital text and text-layer PDFs are parsed locally; photographs, scans and
    image-only PDFs go to Gemini vision. The deterministic date gate downstream
    is what makes reading pixels safe at all: a misread numeral cannot silently
    become a scheduled deadline.
    """
    name = (filename or "").lower()
    is_image = content_type.startswith("image/") or name.endswith(
        (".png", ".jpg", ".jpeg", ".webp", ".heic", ".gif", ".bmp"))

    if is_image:
        mime = content_type if content_type.startswith("image/") else "image/jpeg"
        seen = vision.transcribe_contract_image(raw, mime)
        return seen["text"], {"how": "gemini-vision", "detail": "read from a photograph",
                              "model": seen["model"], "illegible": seen.get("illegible", False),
                              "error": seen.get("error")}

    if name.endswith(".pdf"):
        from pypdf import PdfReader
        try:
            reader = PdfReader(io.BytesIO(raw))
            text = "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            text = ""
        if text.strip():
            return text, {"how": "pdf-text-layer", "detail": "parsed from the PDF text layer"}
        seen = vision.transcribe_contract_image(raw, "application/pdf")
        return seen["text"], {"how": "gemini-vision",
                              "detail": "scanned PDF with no text layer, read by Gemini",
                              "model": seen["model"], "illegible": seen.get("illegible", False),
                              "error": seen.get("error")}

    return raw.decode("utf-8", errors="replace"), {"how": "plain-text", "detail": "read as text"}


@api.post("/contracts/upload")
async def upload_contract(file: UploadFile):
    raw = await file.read()
    text, source = await asyncio.to_thread(
        _read_contract, file.filename or "", raw, file.content_type or "")
    if not text.strip():
        why = source.get("error") or f"nothing readable via {source['how']}"
        raise HTTPException(422, f"could not read any contract text from that file: {why}")
    result = await _intake(text, source=source)
    result["source"] = source
    return result


@api.get("/contracts/{name}")
async def demo_contract(name: str):
    """Serve a bundled sample contract (used by the guided demo)."""
    safe = Path(name).name
    path = Path(__file__).parent / "data" / "contracts" / safe
    if not path.exists():
        raise HTTPException(404, "no such sample contract")
    return FileResponse(path)


@api.get("/obligations/{obligation_id}/invoice")
def invoice_document(obligation_id: int):
    """The invoice document the agent read, exactly as the vendor sent it."""
    ob = ledger.get_obligation(obligation_id)
    if ob is None:
        raise HTTPException(404, "unknown obligation")
    path = inbox.invoice_path(ob["vendor"])
    if not path.exists():
        raise HTTPException(404, "no invoice issued yet")
    return FileResponse(path)


# Operational diagnostics are facts worth keeping, but they belong in the
# access log - the register of actions renders the story, not the plumbing.
_DIAG_KINDS = {"RUN_ATTEMPT_FAILED", "WAKE_DEGRADED", "RUN_INCOMPLETE"}


@api.get("/quota")
async def quota():
    """What model quota this instance believes it still has.

    Free-tier Gemini is 20 requests per day per project per model, so the
    honest unit is the (key, model) pair. Nothing here is a secret: it counts
    buckets, never prints a key.
    """
    return llm.bucket_report()


@api.get("/activity")
def activity():
    return [e for e in ledger.recent_activity()
            if e.get("kind") not in _DIAG_KINDS]


@api.post("/obligations/{obligation_id}/offer")
async def reoffer_approval(obligation_id: int):
    """Resend the pending approval to the phone (demo-friendly, idempotent)."""
    ob = ledger.get_obligation(obligation_id)
    if ob is None:
        raise HTTPException(404, "unknown obligation")
    if ob["status"] != "AWAITING_APPROVAL":
        raise HTTPException(409, "nothing awaiting approval here")
    sent = await approvals.notify_pending(ob)
    return {"offered": sent, "channel": "telegram" if sent else None}


@api.get("/access")
def access_log():
    """What the mailbox watcher looked at, and — the point — what it refused.

    The denominator is the claim: unseen counted, declined hashed, opened
    listed with reasons, all inside hash chain zero.
    """
    import json as _json
    receipts = ledger.get_receipts(0)
    intact, _broken = ledger.verify_chain(0)
    summary = {"scans": 0, "unseen_total": 0, "opened": 0, "declined": 0,
               "discarded_no_renewal_language": 0, "denied_approvals": 0}
    recent = []
    for r in receipts[-80:]:
        payload = _json.loads(r["payload"])
        kind = r["kind"]
        if kind == "MAILBOX_SCANNED":
            summary["scans"] += 1
            # Declined mail is deliberately left UNSEEN (BODY.PEEK), so each
            # scan re-counts it; the honest headline is the latest scan plus
            # the lifetime count of opens.
            summary["unseen_total"] = payload.get("unseen", 0)
            summary["declined"] = payload.get("declined", 0)
            summary["opened"] += payload.get("opened", 0)
        elif kind == "MESSAGE_DISCARDED":
            summary["discarded_no_renewal_language"] += 1
        elif kind == "APPROVAL_DENIED":
            summary["denied_approvals"] += 1
        recent.append({"kind": kind, "ts": r["ts"], "hash": r["hash"],
                       "payload": payload})
    return {"chain_intact": intact, "configured": mailbox.configured(),
            "telegram": approvals.configured(),
            "summary": summary, "recent": recent[-30:]}


@api.get("/obligations/{obligation_id}/pack")
def evidence_pack(obligation_id: int):
    """The printable evidence pack: obligation, clause, gate, full chain."""
    ob = ledger.get_obligation(obligation_id)
    if ob is None:
        raise HTTPException(404, "unknown obligation")
    receipts = ledger.get_receipts(obligation_id)
    intact, broken = ledger.verify_chain(obligation_id)
    import json as _json
    from fastapi.responses import HTMLResponse

    rows = ""
    for i, r in enumerate(receipts, 1):
        payload = _json.dumps(_json.loads(r["payload"]), indent=0)[1:-1].replace('"', "")
        rows += (f"<tr><td class='no'>{i}</td><td class='k'>{r['kind']}</td>"
                 f"<td class='p'>{payload[:400]}</td><td class='t'>{r['ts'][:19]}Z</td>"
                 f"<td class='h'>{r['hash']}</td></tr>")
    verdict_line = ("CHAIN VERIFIED INTACT — every hash recomputed from the genesis record"
                    if intact else f"CHAIN BROKEN AT {broken}")
    html = f"""<!DOCTYPE html><html><head><meta charset='utf-8'>
<title>Evidence pack — obligation {obligation_id}</title><style>
  body {{ font: 13px/1.55 Georgia, serif; color: #191917; margin: 48px auto; max-width: 900px; padding: 0 24px; }}
  .rule {{ border-top: 6px solid #191917; margin-bottom: 8px; }}
  h1 {{ font-size: 24px; font-weight: 600; }} .mono {{ font-family: Consolas, monospace; }}
  .sub {{ color: #6e6b63; font-size: 12px; letter-spacing: 2px; text-transform: uppercase; }}
  .box {{ border: 1px solid #c9c5ba; padding: 14px 18px; margin: 18px 0; }}
  .clause {{ font-style: italic; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 11.5px; margin-top: 10px; }}
  th {{ text-align: left; border-bottom: 2px solid #191917; padding: 6px; font-size: 10px; letter-spacing: 1.5px; text-transform: uppercase; }}
  td {{ border-bottom: 1px solid #e4e2da; padding: 6px; vertical-align: top; }}
  td.h {{ font-family: Consolas, monospace; font-size: 9px; word-break: break-all; color: #6e6b63; }}
  td.no {{ color: #6e6b63; }} td.k {{ font-weight: 700; white-space: nowrap; }}
  td.p {{ color: #444; }} td.t {{ font-family: Consolas, monospace; font-size: 10px; white-space: nowrap; }}
  .verdict {{ font-weight: 700; letter-spacing: 1px; margin: 16px 0; }}
  .ok {{ color: #1e5c3a; }} .bad {{ color: #8c2318; }}
  @media print {{ .noprint {{ display: none; }} }}
</style></head><body>
<div class='rule'></div>
<div class='sub'>Reaper — evidence pack · generated {clock.today().isoformat()}</div>
<h1>{ob['vendor']} — obligation №{str(obligation_id).zfill(4)}</h1>
<div class='box'>
  <div class='sub'>Renewal clause, verbatim</div>
  <p class='clause'>“{ob['clause_text']}”</p>
  <p>Term ends <b>{ob['term_end']}</b> · model read <b class='mono'>{ob['llm_deadline']}</b> ·
  engine derived <b class='mono'>{ob['engine_deadline']}</b> · gate verdict <b>{ob['gate_verdict']}</b> ·
  notice to <span class='mono'>{ob['recipient']}</span> · status <b>{ob['status']}</b></p>
</div>
<div class='sub'>Chain of custody — {len(receipts)} records</div>
<table><tr><th>№</th><th>Record</th><th>Payload</th><th>Time (UTC)</th><th>SHA-256</th></tr>{rows}</table>
<p class='verdict {"ok" if intact else "bad"}'>{verdict_line}</p>
<p class='sub noprint'>print this page to PDF to attach it to a dispute</p>
</body></html>"""
    return HTMLResponse(html)


@api.get("/briefing")
def briefing_deck():
    """A board-ready briefing the agent writes from its own ledger."""
    from fastapi.responses import HTMLResponse
    from reaper import briefing, deck
    facts = briefing.gather()
    story = briefing.narrate(facts)
    return HTMLResponse(deck.render(facts, story))


@api.get("/calendar.ics")
def calendar_feed():
    """Obligation deadlines as an iCalendar feed — subscribe from any calendar."""
    from fastapi.responses import Response
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Reaper//obligations//EN",
             "X-WR-CALNAME:Reaper obligations"]
    for ob in ledger.list_obligations():
        oid = ob["id"]
        if ob.get("engine_deadline"):
            d = ob["engine_deadline"].replace("-", "")
            lines += ["BEGIN:VEVENT", f"UID:reaper-{oid}-notice@reaper",
                      f"DTSTART;VALUE=DATE:{d}",
                      f"SUMMARY:Notice deadline — {ob['vendor']} (Reaper)",
                      f"DESCRIPTION:Non-renewal notice must be delivered by this date. Status: {ob['status']}",
                      "END:VEVENT"]
        if ob.get("term_end"):
            d = ob["term_end"].replace("-", "")
            lines += ["BEGIN:VEVENT", f"UID:reaper-{oid}-term@reaper",
                      f"DTSTART;VALUE=DATE:{d}",
                      f"SUMMARY:Term ends — {ob['vendor']} (Reaper)",
                      "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return Response("\r\n".join(lines), media_type="text/calendar")


async def _open_notice_window(obligation_id: int) -> dict:
    ob = await asyncio.to_thread(ledger.get_obligation, obligation_id)
    # Each wake gets its own session. Reusing one name across attempts meant a
    # run that died left the stored session ahead of what the next run loaded,
    # and every later wake failed on "the session has been modified in storage"
    # - a first failure that quietly poisoned all the ones after it. Which
    # session actually holds the pause is recorded in the resume pointer, so
    # the durable approval still comes back to exactly the right place.
    session_id = f"ob-{obligation_id}-{uuid.uuid4().hex[:8]}"
    result = await _run(
        session_id,
        text=f"NOTICE. The notice window is open for obligation {obligation_id} "
             f"(vendor {ob['vendor']}, deadline {ob['engine_deadline']}). "
             "Draft the notice and request approval.",
    )
    if result["pending"]:
        await asyncio.to_thread(
            ledger.save_resume_pointer,
            obligation_id, USER_ID, result.get("session_id") or session_id,
            result["pending"]["invocation_id"],
            result["pending"]["function_call_id"],
        )
        # Offer the signature where the human actually is: on the phone.
        # Failure is harmless — the in-app approval stands regardless.
        fresh = await asyncio.to_thread(ledger.get_obligation, obligation_id)
        if fresh is not None:
            await approvals.notify_pending(fresh)
    return {"report": result["final_text"], "degraded": result["degraded"],
            "awaiting_approval": result["pending"] is not None}


@api.post("/obligations/{obligation_id}/notice-window")
async def notice_window_open(obligation_id: int):
    """Wake: the notice window is open (API trigger; the ticker does this itself)."""
    ob = ledger.get_obligation(obligation_id)
    if ob is None:
        raise HTTPException(404, "unknown obligation")
    if ob["status"] != "SCHEDULED":
        raise HTTPException(409, f"notice window applies to SCHEDULED "
                                 f"obligations (current: {ob['status']})")
    return await _detached_run(_open_notice_window(obligation_id),
                               obligation_id=obligation_id)


class Decision(BaseModel):
    approve: bool


@api.post("/obligations/{obligation_id}/approval")
async def deliver_approval(obligation_id: int, decision: Decision):
    """The human decision arrives — possibly after a full process restart."""
    ob = ledger.get_obligation(obligation_id)
    if ob is None:
        raise HTTPException(404, "unknown obligation")
    if ob["status"] != "AWAITING_APPROVAL" and             ledger.get_resume_pointer(obligation_id) is None:
        raise HTTPException(409, "no pending approval for this obligation")
    return await _detached_run(
        _deliver_decision(obligation_id, decision.approve, via={"channel": "app"}),
        obligation_id=obligation_id,
    )


async def _deliver_decision(obligation_id: int, approve: bool, via: dict) -> dict | None:
    """One decision path for every channel — app, Telegram, whatever comes.

    WHO authorised the notice and THROUGH WHICH CHANNEL is exactly the question
    a vendor will raise later, so the channel goes into the APPROVED receipt.
    Returns None when nothing is pending.
    """
    # The tool marks the obligation AWAITING_APPROVAL from inside the run, but
    # the resume pointer is only written once that run yields. Wait out that
    # gap rather than rejecting an approval that is about to become valid.
    ptr = ledger.get_resume_pointer(obligation_id)
    for _ in range(30):
        if ptr is not None:
            break
        ob = ledger.get_obligation(obligation_id)
        if ob is None or ob["status"] != "AWAITING_APPROVAL":
            break
        await asyncio.sleep(0.5)
        ptr = ledger.get_resume_pointer(obligation_id)
    if ptr is None:
        ob = ledger.get_obligation(obligation_id)
        if ob is not None and ob["status"] == "AWAITING_APPROVAL":
            # The process died between AWAITING_APPROVAL and the resume pointer
            # becoming durable, so this pause can never be resumed. Heal instead
            # of stranding: receipt the fact, fall back to SCHEDULED, and re-open
            # the notice window so a fresh durable pause (and a fresh phone
            # offer) replaces the lost one. The human signs the reissued request.
            ledger.append_receipt(obligation_id, "APPROVAL_RESET", {
                "reason": "process died before the paused run became durable",
                "action": "notice window re-opened; a fresh approval will be offered",
            })
            ledger.set_status(obligation_id, "SCHEDULED")
            return await _open_notice_window(obligation_id)
        return None
    # The decision is recorded once, even if delivering it takes several
    # attempts: an evidence chain with two APPROVED entries for one signature
    # is a defect, not a detail.
    kind = "APPROVED" if approve else "REJECTED"
    already = any(r["kind"] in ("APPROVED", "REJECTED")
                  for r in ledger.get_receipts(obligation_id))
    if not already:
        ledger.append_receipt(obligation_id, kind, via)
    response_part = types.Part(
        function_response=types.FunctionResponse(
            id=ptr["function_call_id"],
            name="request_notice_approval",
            response={
                "status": "approved" if approve else "rejected",
                "obligation_id": obligation_id,
            },
        )
    )
    result = await _run(
        ptr["session_id"],
        parts=[response_part],
        invocation_id=ptr["invocation_id"],
    )
    # Only now is the decision truly delivered. If the resume died mid-flight
    # the pointer survives, so the approval can be retried instead of being
    # stranded with the obligation waiting forever.
    if not result["degraded"]:
        ledger.clear_resume_pointer(obligation_id)
    return {"report": result["final_text"], "degraded": result["degraded"],
            "obligation": ledger.get_obligation(obligation_id)}


async def _process_invoice(obligation_id: int) -> dict:
    result = await _run(
        f"verify-{obligation_id}-{uuid.uuid4().hex[:8]}",
        text=f"VERIFY. The next invoice arrived for obligation {obligation_id}. "
             "Check it and act accordingly.",
    )
    # A refuted verdict is arithmetic, so it must not depend on the agent
    # getting one more turn: file the dispute if the run ended without it.
    await asyncio.to_thread(tools.ensure_dispute_filed, obligation_id)
    return {"report": result["final_text"], "degraded": result["degraded"],
            "obligation": ledger.get_obligation(obligation_id)}


@api.post("/obligations/{obligation_id}/invoice-arrived")
async def invoice_arrived(obligation_id: int):
    """Wake: the vendor's next-cycle invoice landed (API trigger; ticker does this itself)."""
    ob = ledger.get_obligation(obligation_id)
    if ob is None:
        raise HTTPException(404, "unknown obligation")
    if ob["status"] != "NOTICE_SENT":
        raise HTTPException(409, f"invoice verification applies to NOTICE_SENT "
                                 f"obligations (current: {ob['status']})")
    return await _detached_run(_process_invoice(obligation_id),
                               obligation_id=obligation_id)


@api.get("/clock")
def get_clock():
    return {"ledger_date": clock.today().isoformat(),
            "offset_days": clock.offset_days(),
            "wake_lead_days": WAKE_LEAD_DAYS}


class Advance(BaseModel):
    days: int | None = None  # None = jump to the eve of the next event


def _next_event_gap() -> int:
    """Days until the next thing the agent would act on (min 1)."""
    today = clock.today()
    gaps = []
    for ob in ledger.list_obligations():
        if ob["status"] == "SCHEDULED" and ob["engine_deadline"]:
            wake = date.fromisoformat(ob["engine_deadline"]) - timedelta(days=WAKE_LEAD_DAYS)
            gaps.append((wake - today).days)
        elif ob["status"] == "NOTICE_SENT":
            gaps.append((date.fromisoformat(ob["term_end"]) - today).days + 1)
    future = [g for g in gaps if g > 0]
    return min(future) if future else 1


@api.post("/clock/advance")
async def clock_advance(adv: Advance):
    days = adv.days if adv.days and adv.days > 0 else _next_event_gap()
    new_date = clock.advance(days)
    return {"ledger_date": new_date.isoformat(), "advanced_days": days}


@api.post("/chaos/kill")
async def chaos_kill():
    """Kill the agent process mid-flight. The point: state survives, we don't.

    Locally the supervisor loop restarts the process; on Cloud Run the next
    request cold-starts a fresh instance. Either way the ledger, the clock and
    any paused approval come back intact.
    """
    asyncio.get_event_loop().call_later(0.4, os._exit, 1)
    return {"dying": True, "note": "process exiting; durable state will survive"}


# The dashboard asks for the docket about once a second, and every ask used to
# be a fresh read of every obligation. Over a long demo that is tens of
# thousands of reads for a list that changes a few times a minute - enough to
# spend a day's free-tier Firestore allowance and take the ledger down with it.
# A one-second cache changes nothing anyone can see and removes the problem.
_DOCKET_TTL_S = float(os.getenv("REAPER_DOCKET_TTL_S", "1.0"))
_docket_cache: dict = {"at": 0.0, "rows": None}


def _docket() -> list:
    now = time.monotonic()
    if _docket_cache["rows"] is None or now - _docket_cache["at"] > _DOCKET_TTL_S:
        _docket_cache["rows"] = ledger.list_obligations()
        _docket_cache["at"] = now
    return _docket_cache["rows"]


def _docket_changed() -> None:
    """Anything that writes the ledger invalidates the cached docket."""
    _docket_cache["rows"] = None


@api.get("/obligations")
def obligations():
    return _docket()


@api.get("/obligations/{obligation_id}/receipts")
def receipts(obligation_id: int):
    intact, broken = ledger.verify_chain(obligation_id)
    return {"chain_intact": intact, "first_broken": broken,
            "receipts": ledger.get_receipts(obligation_id)}


@api.get("/obligations/{obligation_id}/precedents")
async def obligation_precedents(obligation_id: int):
    """Advisory recall: how clauses shaped like this one resolved before.

    Read-only over the BigQuery precedent store; the consultation itself is
    hash-chained as a PRECEDENT_CONSULTED receipt. Never affects any verdict.
    """
    result = await asyncio.to_thread(tools.recall_precedent, obligation_id)
    if "error" in result:
        raise HTTPException(404, result["error"])
    return result


@api.get("/precedents/status")
async def precedents_status():
    """Health of the precedent store (BigQuery sandbox table)."""
    from reaper import precedent

    return await asyncio.to_thread(precedent.table_status)


@api.get("/healthz")
async def healthz():
    return {"ok": True}
