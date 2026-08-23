# Testing Reaper

Everything below runs on a laptop with **only a free Gemini API key** — no
billing account, no Google Cloud project, no Telegram, no mailbox. Those are
optional live channels documented at the end.

## What you need

- Python 3.11+ (3.12 recommended)
- A Gemini API key: https://aistudio.google.com/apikey

## 60-second spin-up

```bash
git clone https://github.com/N-45div/Reaper.git && cd Reaper
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt        # Linux/Mac: .venv/bin/pip
cp .env.example .env                                 # then set GOOGLE_API_KEY
.venv/Scripts/uvicorn main:api --port 8080           # Linux/Mac: .venv/bin/uvicorn
```

Open **http://localhost:8080** for the landing page, **/app** for the ledger.
Keep `REAPER_LEDGER=sqlite` (the default) — the evidence chain then lives in a
local file and every claim below is still checkable.

> To exercise the chaos-kill path with automatic resurrection, start the
> server with `.\run_local.ps1` instead (a one-line supervisor loop). Plain
> uvicorn works for everything else — you just restart it by hand.

## Walkthrough 1 — the villain path (the demo video's arc)

**1. File the contract.**

```bash
curl -F "file=@data/contracts/datavault-pro-services.txt" localhost:8080/contracts/upload
```

Expect: obligation `1` for DataVault Pro, status **`SCHEDULED`**, gate verdict
**`MATCH`** — the model's deadline and the date engine's independent
derivation both read `2026-11-30`. The UI shows both readings side by side.

**2. Open the notice window.**

```bash
curl -X POST localhost:8080/obligations/1/notice-window
```

Expect: status **`AWAITING_APPROVAL`** — the run is now paused mid-plan on a
`LongRunningFunctionTool`, waiting for a human.

**3. Kill the process. This is the point.**

If running under `run_local.ps1`:

```bash
curl -X POST localhost:8080/chaos/kill      # hard os._exit(1), supervisor resurrects it
```

With plain uvicorn: Ctrl+C the server, then start it again.

Either way, the new process is a different PID with empty RAM. Check
`GET /obligations` — the obligation is still `AWAITING_APPROVAL`. The pause
survived because the invocation lives in `DatabaseSessionService`, not memory.

**4. Sign.**

```bash
curl -X POST localhost:8080/obligations/1/approval -H "Content-Type: application/json" -d '{"approve": true}'
```

Expect: the paused run **resumes from the exact suspension point** (in the
restarted process) and status becomes **`NOTICE_SENT`**. Without SMTP
configured the delivery is simulated and labelled as such in the receipt.

**5. The vendor's next invoice arrives.**

```bash
curl -X POST localhost:8080/obligations/1/invoice-arrived
```

Expect: DataVault bills anyway (that's the fixture's villainy) — the
arithmetic check `billed vs expected` fails, status becomes **`DISPUTED`**,
and a `DISPUTE_OPENED` receipt records the notice receipt's hash as attached
evidence.

**6. Audit the chain.**

```bash
curl localhost:8080/obligations/1/receipts
```

Expect: `"chain_intact": true` and the full receipt sequence —
`EXTRACTED → GATED → REDACTED/READ_AS → WOKE → APPROVAL_REQUESTED →
NOTICE_SENT → INVOICE_CHECKED → DISPUTE_OPENED` — each entry carrying
`sha256(previous hash | kind | payload | timestamp)`. Edit any row in the
database and this endpoint will tell you exactly where the chain breaks.

Also inspect:

- `GET /obligations/1/pack` — the printable evidence pack (chain of custody,
  both deadline derivations, integrity verdict recomputed from record №1)
- `GET /calendar.ics` — the obligations as a subscribable calendar
- `GET /access` — chain 0, the mailbox access log
- `POST /demo/reset` — wipe state and start over

## Walkthrough 2 — the trap clause

```bash
curl -F "file=@data/contracts/ambiguous-hostwave.txt" localhost:8080/contracts/upload
```

This contract's clause reads **"sixty (90) days"** — the written word and the
numeral disagree. Expect: gate verdict **`AMBIGUOUS`**, status **`BLOCKED`**,
no deadline scheduled. Reaper refuses to gamble on a contradiction; a human
must resolve it.

## Walkthrough 3 — a photographed contract

```bash
curl -F "file=@data/contracts/northwind-facilities-photo.jpg" localhost:8080/contracts/upload
```

Gemini vision reads the photograph; the `READ_AS` receipt records that the
text came from an image, and the same deterministic gate applies to whatever
was read.

There is also a PII-laden fixture, `sterling-analytics-payments.txt` (card
number, PAN, phone). File it and check its `REDACTED` receipt: the model
received masked text only, and the receipt records how many identifiers were
hidden — never the values.

## The test suite

```bash
.venv/Scripts/python -m pytest tests -q
```

**35 deterministic tests** — no network, no API key needed — covering the
date engine (word-vs-numeral conflicts, renewal-term exclusion, anchor
handling), delivery-method classification, privacy redaction
(Luhn/Verhoeff validation), mailbox admission rules, and ledger hash-chain
integrity including tamper detection.

## The agent exam

```bash
.venv/Scripts/pip install "google-adk[eval]"
.venv/Scripts/python scripts/make_evalset.py
REAPER_LEDGER=sqlite .venv/Scripts/adk eval reaper evals/reaper.evalset.json --config_file_path evals/eval_config.json
```

Two live cases scored with a deterministic ROUGE metric (no LLM judge): the
clean clause must gate `MATCH` and schedule; the trap clause must come back
`BLOCKED`. Expected result: **2/2 passed**.

## Optional live channels

Each is independent; configure only what you want to verify. All settings are
documented in `.env.example`.

| Channel | Enable with | What it proves |
|---|---|---|
| Firestore ledger | `REAPER_LEDGER=firestore`, `GOOGLE_CLOUD_PROJECT`, ADC (`gcloud auth application-default login`) | the same hash chain, in Google Cloud — receipts visible in the Firestore console |
| Telegram approvals | `REAPER_TELEGRAM_TOKEN`, `REAPER_TELEGRAM_CHAT_ID` | the signature arrives on a phone; taps from unenrolled chats are logged as `APPROVAL_DENIED` |
| Owned mailbox | `REAPER_MAIL_USER`, `REAPER_MAIL_APP_PASSWORD`, `REAPER_OWNER_EMAIL`, `REAPER_VENDOR_DOMAINS` | headers-first intake; every open and every refusal appears in chain 0 (`GET /access`) |
| Real SMTP notices | same mailbox credentials | the notice's `Message-ID` is hash-stamped as delivery evidence |

## Notes & troubleshooting

- The uvicorn target is **`main:api`** (not `main:app`).
- The DB URL must keep the async driver (`sqlite+aiosqlite://`) — plain
  `sqlite://` fails with `DatabaseSessionService`.
- Gemini API quota is granted per project and per model. If a run hits the
  daily cap, add extra keys via `GOOGLE_API_KEYS` (comma-separated) — Reaper
  rotates keys and steps down a model ladder instead of stopping. When every
  attempt fails, the ledger records that the document could not be read; an
  API failure is never converted into a contractual verdict.
- Demo time: `POST /clock/advance` moves the simulated calendar (the UI's
  "let time pass"), which is how months of dormancy fit in a demo. The clock
  offset is itself persisted state.
