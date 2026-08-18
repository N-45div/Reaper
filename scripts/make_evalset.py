"""Generate the Reaper eval exam from ADK's own pydantic models.

Two cases prove the two behaviors that define the product:
  1. a clean clause is gated MATCH and scheduled with the right deadline
  2. the planted words-vs-numerals trap is caught and BLOCKED, honestly

Run:  python scripts/make_evalset.py   then   adk eval reaper evals/reaper.evalset.json
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.adk.evaluation.eval_case import EvalCase, Invocation, SessionInput
from google.adk.evaluation.eval_set import EvalSet
from google.genai import types

ROOT = Path(__file__).resolve().parent.parent
CONTRACTS = ROOT / "data" / "contracts"
OUT = ROOT / "evals"


def _intake_message(contract_file: str) -> types.Content:
    text = (CONTRACTS / contract_file).read_text(encoding="utf-8")
    return types.Content(role="user", parts=[types.Part(
        text="INTAKE. Extract the auto-renewal obligation from this contract "
             f"and gate-schedule it.\n\n--- CONTRACT ---\n{text}"
    )])


def _reference(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


def case(eval_id: str, contract: str, reference: str) -> EvalCase:
    return EvalCase(
        eval_id=eval_id,
        conversation=[Invocation(
            invocation_id=f"{eval_id}-1",
            user_content=_intake_message(contract),
            final_response=_reference(reference),
        )],
        session_input=SessionInput(app_name="reaper", user_id="examiner", state={}),
        creation_timestamp=time.time(),
    )


evalset = EvalSet(
    eval_set_id="reaper_exam",
    name="Reaper gate exam",
    description="Proves the deterministic gate schedules clean clauses and blocks unverifiable ones.",
    eval_cases=[
        case(
            "clean_clause_is_scheduled",
            "cloudco-metrics-msa.txt",
            "I have processed the contract intake and scheduled the auto-renewal "
            "obligation.\n\n### Auto-Renewal Clause Verbatim\n"
            '> "This Agreement shall automatically renew for successive twelve '
            "(12) month terms unless either party provides written notice of "
            "non-renewal at least sixty (60) days prior to the end of the "
            "then-current term. Notice of non-renewal must be delivered in "
            'writing to cancellations@cloudcometrics.test."\n\n'
            "### Extracted Metadata\n"
            "* **Vendor:** CloudCo Metrics\n"
            "* **Current Term End Date:** December 31, 2026\n"
            "* **Notice Deadline:** November 1, 2026 (60 days prior to term end)\n"
            "* **Recipient:** cancellations@cloudcometrics.test\n\n"
            "The deterministic engine verified the notice deadline matches, and "
            "the obligation has been scheduled.\n"
            "**Status:** SCHEDULED | **Next Step:** Monitor for the notice window.",
        ),
        case(
            "conflicting_clause_is_blocked",
            "ambiguous-hostwave.txt",
            "### Intake Analysis\n\n1. **Auto-Renewal Clause (Verbatim)**:\n"
            '> "This Agreement renews automatically for additional one-year '
            "terms. Subscriber may opt out of renewal by providing written "
            "notice no later than sixty (90) days prior to the end of the "
            'then-current term, sent to support@hostwave.test."\n\n'
            "2. **Extracted Metadata**:\n"
            "* **Vendor**: HostWave Systems\n"
            "* **Recipient**: support@hostwave.test\n\n"
            "### Gate & Schedule Result\n\n"
            "The gate returned an **AMBIGUOUS** verdict.\n"
            "* **Gate Verdict**: AMBIGUOUS\n"
            "* **Engine Reason**: Conflicting written and numeric values in "
            "'sixty (90) days'.\n"
            "* **Status**: BLOCKED\n\n"
            "**Recommendation**: The contract contains a direct contradiction "
            "between the written word and the numeral; scheduling is blocked "
            "and human review is required. No notice was scheduled on an "
            "unverified date.",
        ),
    ],
    creation_timestamp=time.time(),
)

OUT.mkdir(exist_ok=True)
out_file = OUT / "reaper.evalset.json"
out_file.write_text(evalset.model_dump_json(indent=2), encoding="utf-8")
print(f"wrote {out_file} with {len(evalset.eval_cases)} cases")
