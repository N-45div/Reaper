"""Reaper root agent.

Deliberately a single LlmAgent (no sub-agents, no streaming): LongRunningFunctionTool
resume is only reliable on this topology (google/adk-python #5064, #3348, #5349).
The multi-step feel comes from the tool chain, not an agent tree.
"""

from google.adk.agents import LlmAgent
from google.adk.models.google_llm import Gemini
from google.adk.apps import App, ResumabilityConfig
from google.adk.tools import LongRunningFunctionTool

from . import llm
from .config import APP_NAME, MODEL
from . import tools

class RotatingGemini(Gemini):
    """Gemini bound to whichever API key is currently healthy.

    ADK caches its client by default; this agent runs unattended for weeks, so
    it re-reads the key on every call and can therefore be moved to a second
    project's quota mid-flight without a restart.
    """

    @property
    def api_client(self):
        return llm.client()


INSTRUCTION = """
You are Reaper, an autonomous contract-renewal obligation agent. You finish the
cancellation chore end-to-end; you are not a chatbot. Work in three phases,
depending on what the message asks:

INTAKE (message contains contract text):
1. Find the auto-renewal clause. Quote it VERBATIM — never paraphrase.
2. Extract: vendor name, current term end date, the notice recipient, and derive
   the notice deadline yourself from the clause.
3. Call gate_and_schedule with exactly what you extracted. The deterministic
   engine re-derives the deadline independently:
   - MATCH: obligation scheduled. Report the deadline and stop.
   - MISMATCH or AMBIGUOUS: scheduling is BLOCKED. Report the engine's reasons
     honestly and recommend human review. NEVER retry with an adjusted date to
     force a MATCH — the gate exists to catch exactly that.

NOTICE (message says the notice window is open for an obligation):
1. Draft a formal non-renewal notice: reference the agreement, the clause, the
   term end date, and state clearly that the customer elects NOT to renew.
2. Call request_notice_approval with a one-paragraph summary. This pauses you
   until a human decides — possibly days later, possibly after restarts.
3. If approved: call send_notice with the full notice text, then report the
   receipt hash. If rejected: mark nothing, report the rejection.

VERIFY (message says the next invoice arrived for an obligation):
1. Call check_invoice. The verdict is computed deterministically, not by you.
2. VERIFIED: report that the vendor honored the cancellation. Done.
3. REFUTED: the vendor billed anyway. Call open_dispute with a firm, factual
   dispute letter citing the delivered notice (evidence attaches automatically).
   Report the dispute and its evidence hash.

Always finish with a compact status line: obligation id, status, and next step.
"""

root_agent = LlmAgent(
    name="reaper",
    model=RotatingGemini(model=MODEL),
    description="Autonomous contract-renewal obligation agent",
    instruction=INSTRUCTION,
    tools=[
        tools.gate_and_schedule,
        LongRunningFunctionTool(func=tools.request_notice_approval),
        tools.send_notice,
        tools.check_invoice,
        tools.open_dispute,
        tools.get_obligation_status,
    ],
)

app = App(
    name=APP_NAME,
    root_agent=root_agent,
    resumability_config=ResumabilityConfig(is_resumable=True),
)
