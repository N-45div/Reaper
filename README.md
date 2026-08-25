# Reaper

**The agent that finishes the cancellation chore — and checks that the vendor obeyed.**

Every renewal tool on the market stops at reminding a human. Reaper sleeps for
weeks as a durable timer, wakes inside the contractual notice window, sends the
actual non-renewal notice after one human approval, hash-stamps the delivery
receipt — then reads the *next invoice* to verify the vendor really stopped
billing. If they billed anyway, it opens the dispute itself, attaching its own
timestamped receipt as evidence.

And it refuses to trust its own AI: a **deterministic date engine**
independently re-derives every notice deadline from the raw clause text. Any
mismatch with the LLM's reading **blocks scheduling** — no notice is ever
queued on an unverified date.

## How it works

```
contract PDF ──► INTAKE      Gemini extracts the renewal clause verbatim;
                             the date engine re-derives the deadline.
                             MATCH → scheduled · MISMATCH/AMBIGUOUS → BLOCKED
        ⏸ agent sleeps until the notice window (zero tokens burned)
notice window ─► NOTICE      drafts the formal non-renewal notice, pauses
                             durably for ONE human approval — the pause
                             survives full process restarts
        ✉ notice delivered, receipt SHA-256-chained into the ledger
next invoice ──► VERIFY      deterministic check: billed vs expected
                             VERIFIED → done · REFUTED → dispute opened with
                             the delivery receipt attached as evidence
```

Built on **google-adk 2.6.3** (single resumable `LlmAgent`,
`LongRunningFunctionTool` + `ResumabilityConfig` + `DatabaseSessionService`),
**Gemini 3.5 Flash** with a **Gemma 4** triage filter, FastAPI, and Google
Cloud: the evidence chain lives in **Firestore** (hash-chained receipts,
activity register, clock state), with Cloud Scheduler → Pub/Sub as the
production slot for the wake ticker.

**[Architecture deep-dive →](ARCHITECTURE.md)** — the system map, the
autonomous arc, the durable pause, the hash-chained evidence ledger, and the
trust boundaries, all as diagrams.

**[Try the live demo →](https://reaper-sxxs.onrender.com)** — a hosted test
instance. It writes to the same Firestore evidence ledger shown in the demo
video. Compute runs on Render; the evidence chain lives on Google Cloud.

**[Reproducible testing guide →](TESTING.md)** — spin-up, three guided
walkthroughs (villain path, trap clause, photographed contract), the
kill-the-process test, the 35-test suite, and the agent exam.

## Run it locally

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
cp .env.example .env      # add your Gemini API key (aistudio.google.com/apikey)
.venv/Scripts/uvicorn main:api --port 8080
```

Then walk the whole loop:

```bash
# 1. INTAKE — upload a contract (three demo contracts in data/contracts/)
curl -F "file=@data/contracts/cloudco-metrics-msa.txt" localhost:8080/contracts/upload

# 2. NOTICE — the notice window opens (in prod: Cloud Scheduler fires this)
curl -X POST localhost:8080/obligations/1/notice-window

#    ── kill the server here and restart it: the pending approval survives ──

# 3. The human decision arrives
curl -X POST localhost:8080/obligations/1/approval -H "Content-Type: application/json" -d '{"approve": true}'

# 4. VERIFY — the vendor's next invoice lands
curl -X POST localhost:8080/obligations/1/invoice-arrived

# 5. Inspect the hash-chained evidence trail
curl localhost:8080/obligations/1/receipts
```

Upload `data/contracts/datavault-pro-services.txt` for the villain path (vendor
bills anyway → autonomous dispute), and `ambiguous-hostwave.txt` to watch the
deterministic gate block a clause whose written words and numerals disagree.

## Tests & the eval exam

```bash
.venv/Scripts/python -m pytest tests -q          # 35 deterministic unit tests
.venv/Scripts/python scripts/make_evalset.py     # regenerate the ADK eval set
REAPER_LEDGER=sqlite .venv/Scripts/adk eval reaper evals/reaper.evalset.json --config_file_path evals/eval_config.json
```

The eval exam runs the live agent against two contracts and scores it with a
deterministic ROUGE metric (no LLM judge — fitting, for a product whose thesis
is "don't trust the model's own reading"): the clean clause must be gated
MATCH and scheduled; the planted words-vs-numerals trap must come back BLOCKED.
