"""Reaper service: contract intake, notice-window wakes, approval webhook, evidence API.

The approval flow is the demo centerpiece: request_notice_approval pauses the
run durably (DatabaseSessionService); the pointer rows in the ledger let a
freshly restarted process resume the exact paused invocation.
"""

import asyncio
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

from reaper import clock, inbox, ledger
from reaper.agent import app as adk_app
from reaper.config import APP_NAME, DB_URL
from reaper.triage import triage_contract

STATIC_DIR = Path(__file__).parent / "reaper" / "static"

USER_ID = "owner"
WAKE_LEAD_DAYS = int(os.getenv("REAPER_WAKE_LEAD_DAYS", "7"))
TICK_SECONDS = int(os.getenv("REAPER_TICK_SECONDS", "6"))
runner: Runner | None = None
_in_flight: set[int] = set()


async def _ticker():
    """The agent's own heartbeat: watch the ledger calendar and act unprompted.

    In production this is a Cloud Scheduler -> Pub/Sub wake; locally the same
    logic runs on an in-process loop so the autonomy is visible in the demo.
    """
    while True:
        await asyncio.sleep(TICK_SECONDS)
        today = clock.today()
        for ob in ledger.list_obligations():
            oid = ob["id"]
            if oid in _in_flight:
                continue
            try:
                if ob["status"] == "SCHEDULED" and ob["engine_deadline"]:
                    deadline = date.fromisoformat(ob["engine_deadline"])
                    if today >= deadline - timedelta(days=WAKE_LEAD_DAYS):
                        _in_flight.add(oid)
                        ledger.append_receipt(oid, "WOKE", {
                            "reason": "calendar entered the notice window",
                            "ledger_date": today.isoformat(),
                            "deadline": ob["engine_deadline"],
                        })
                        await _open_notice_window(oid)
                elif ob["status"] == "NOTICE_SENT":
                    term_end = date.fromisoformat(ob["term_end"])
                    if today > term_end:
                        _in_flight.add(oid)
                        ledger.append_receipt(oid, "WOKE", {
                            "reason": "term ended; next-cycle invoice due",
                            "ledger_date": today.isoformat(),
                        })
                        await _process_invoice(oid)
            except Exception:
                pass  # transient (e.g. model quota); retried on a later tick
            finally:
                _in_flight.discard(oid)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global runner
    session_service = DatabaseSessionService(db_url=DB_URL)
    runner = Runner(app=adk_app, session_service=session_service)
    tick_task = asyncio.create_task(_ticker())
    yield
    tick_task.cancel()


api = FastAPI(title="Reaper", lifespan=lifespan)


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
    kwargs = {"user_id": USER_ID, "session_id": session_id, "new_message": content}
    if invocation_id:
        kwargs["invocation_id"] = invocation_id

    final_text, pending, degraded = None, None, None
    try:
        async for event in runner.run_async(**kwargs):
            pending = _pending_approval(event) or pending
            final_text = _final_text(event) or final_text
    except Exception as exc:  # model quota/transient errors mid-run
        # Tool effects already committed are in the ledger; the session is in
        # SQL. Nothing is lost — report the ledger truth instead of a 500.
        degraded = (
            f"model call failed mid-run ({type(exc).__name__}); completed tool "
            "actions are persisted in the ledger and the session is resumable"
        )
    return {"final_text": final_text, "pending": pending, "degraded": degraded}


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


@api.get("/app")
async def dashboard():
    return FileResponse(STATIC_DIR / "index.html")


@api.post("/demo/reset")
async def demo_reset():
    ledger.reset_all()
    inbox.reset_world()
    return {"ok": True}


async def _intake(text: str) -> dict:
    # Gemma triage: skip the expensive agent run when there is no renewal
    # clause at all. Fails open — triage never blocks a real contract.
    triage = triage_contract(text)
    if triage["ok"] and not triage["has_renewal"]:
        return {"session_id": None, "triage": triage, "degraded": None,
                "report": "Triage: no auto-renewal clause found in this "
                          f"document ({triage['model']}). Nothing to schedule.",
                "obligations": ledger.list_obligations()}

    session_id = f"intake-{uuid.uuid4().hex[:8]}"
    result = await _run(
        session_id,
        text="INTAKE. Extract the auto-renewal obligation from this contract "
             f"and gate-schedule it.\n\n--- CONTRACT ---\n{text}",
    )
    return {"session_id": session_id, "report": result["final_text"],
            "triage": triage, "degraded": result["degraded"],
            "obligations": ledger.list_obligations()}


@api.post("/contracts/upload")
async def upload_contract(file: UploadFile):
    raw = await file.read()
    if file.filename.lower().endswith(".pdf"):
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    else:
        text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        raise HTTPException(422, "could not extract any text from the file")
    return await _intake(text)


@api.get("/activity")
async def activity():
    return ledger.recent_activity()


async def _open_notice_window(obligation_id: int) -> dict:
    ob = ledger.get_obligation(obligation_id)
    session_id = f"ob-{obligation_id}"
    result = await _run(
        session_id,
        text=f"NOTICE. The notice window is open for obligation {obligation_id} "
             f"(vendor {ob['vendor']}, deadline {ob['engine_deadline']}). "
             "Draft the notice and request approval.",
    )
    if result["pending"]:
        ledger.save_resume_pointer(
            obligation_id, USER_ID, session_id,
            result["pending"]["invocation_id"],
            result["pending"]["function_call_id"],
        )
    return {"report": result["final_text"], "degraded": result["degraded"],
            "awaiting_approval": result["pending"] is not None}


@api.post("/obligations/{obligation_id}/notice-window")
async def notice_window_open(obligation_id: int):
    """Wake: the notice window is open (API trigger; the ticker does this itself)."""
    if ledger.get_obligation(obligation_id) is None:
        raise HTTPException(404, "unknown obligation")
    return await _open_notice_window(obligation_id)


class Decision(BaseModel):
    approve: bool


@api.post("/obligations/{obligation_id}/approval")
async def deliver_approval(obligation_id: int, decision: Decision):
    """The human decision arrives — possibly after a full process restart."""
    ptr = ledger.pop_resume_pointer(obligation_id)
    if ptr is None:
        raise HTTPException(409, "no pending approval for this obligation")
    ledger.append_receipt(
        obligation_id, "APPROVED" if decision.approve else "REJECTED", {}
    )
    response_part = types.Part(
        function_response=types.FunctionResponse(
            id=ptr["function_call_id"],
            name="request_notice_approval",
            response={
                "status": "approved" if decision.approve else "rejected",
                "obligation_id": obligation_id,
            },
        )
    )
    result = await _run(
        ptr["session_id"],
        parts=[response_part],
        invocation_id=ptr["invocation_id"],
    )
    return {"report": result["final_text"],
            "obligation": ledger.get_obligation(obligation_id)}


async def _process_invoice(obligation_id: int) -> dict:
    result = await _run(
        f"verify-{obligation_id}",
        text=f"VERIFY. The next invoice arrived for obligation {obligation_id}. "
             "Check it and act accordingly.",
    )
    return {"report": result["final_text"], "degraded": result["degraded"],
            "obligation": ledger.get_obligation(obligation_id)}


@api.post("/obligations/{obligation_id}/invoice-arrived")
async def invoice_arrived(obligation_id: int):
    """Wake: the vendor's next-cycle invoice landed (API trigger; ticker does this itself)."""
    if ledger.get_obligation(obligation_id) is None:
        raise HTTPException(404, "unknown obligation")
    return await _process_invoice(obligation_id)


@api.get("/clock")
async def get_clock():
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


@api.get("/obligations")
async def obligations():
    return ledger.list_obligations()


@api.get("/obligations/{obligation_id}/receipts")
async def receipts(obligation_id: int):
    intact, broken = ledger.verify_chain(obligation_id)
    return {"chain_intact": intact, "first_broken": broken,
            "receipts": ledger.get_receipts(obligation_id)}


@api.get("/healthz")
async def healthz():
    return {"ok": True}
