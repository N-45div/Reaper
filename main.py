"""Reaper service: contract intake, notice-window wakes, approval webhook, evidence API.

The approval flow is the demo centerpiece: request_notice_approval pauses the
run durably (DatabaseSessionService); the pointer rows in the ledger let a
freshly restarted process resume the exact paused invocation.
"""

import io
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, UploadFile
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types
from pydantic import BaseModel

from reaper import ledger
from reaper.agent import app as adk_app
from reaper.config import APP_NAME, DB_URL

USER_ID = "owner"
runner: Runner | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global runner
    session_service = DatabaseSessionService(db_url=DB_URL)
    runner = Runner(app=adk_app, session_service=session_service)
    yield


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

    session_id = f"intake-{uuid.uuid4().hex[:8]}"
    result = await _run(
        session_id,
        text="INTAKE. Extract the auto-renewal obligation from this contract "
             f"and gate-schedule it.\n\n--- CONTRACT ---\n{text}",
    )
    return {"session_id": session_id, "report": result["final_text"],
            "obligations": ledger.list_obligations()}


@api.post("/obligations/{obligation_id}/notice-window")
async def notice_window_open(obligation_id: int):
    """Wake: the notice window is open (Cloud Scheduler / demo trigger)."""
    ob = ledger.get_obligation(obligation_id)
    if ob is None:
        raise HTTPException(404, "unknown obligation")
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
    return {"report": result["final_text"],
            "awaiting_approval": result["pending"] is not None}


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


@api.post("/obligations/{obligation_id}/invoice-arrived")
async def invoice_arrived(obligation_id: int):
    """Wake: the vendor's next-cycle invoice landed — verify and act."""
    if ledger.get_obligation(obligation_id) is None:
        raise HTTPException(404, "unknown obligation")
    result = await _run(
        f"verify-{obligation_id}",
        text=f"VERIFY. The next invoice arrived for obligation {obligation_id}. "
             "Check it and act accordingly.",
    )
    return {"report": result["final_text"], "degraded": result["degraded"],
            "obligation": ledger.get_obligation(obligation_id)}


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
