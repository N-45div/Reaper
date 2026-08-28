import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Hosted deploys have no metadata server: accept the Firestore service-account
# key as an env var (raw JSON or base64) and hand it to google-auth via a file.
_sa_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
if _sa_json and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
    import base64
    import tempfile
    raw = _sa_json.strip()
    if not raw.startswith("{"):
        raw = base64.b64decode(raw).decode("utf-8")
    _sa_path = Path(tempfile.gettempdir()) / "reaper-sa.json"
    _sa_path.write_text(raw, encoding="utf-8")
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(_sa_path)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

DB_URL = os.getenv("REAPER_DB_URL", f"sqlite+aiosqlite:///{(DATA_DIR / 'reaper.db').as_posix()}")
MODEL = os.getenv("REAPER_MODEL", "gemini-3.5-flash")

# Gemini API quota is granted per project AND per model, so when one model's
# daily budget runs dry the agent steps down this ladder rather than stopping.
# Every entry is Gemini 3.5 or newer, as the work requires.
MODEL_LADDER = [m.strip() for m in os.getenv(
    "REAPER_MODEL_LADDER",
    "gemini-3.5-flash,gemini-3.6-flash,gemini-3.5-flash-lite,gemini-3.7-flash",
).split(",") if m.strip()]
TRIAGE_MODEL = os.getenv("REAPER_TRIAGE_MODEL", "gemma-4-26b-a4b-it")
# Speech is its own model, not a mode of the reasoning one: a transcriber
# that only transcribes cannot be talked into summarising the call.
TRANSCRIBE_MODEL = os.getenv("REAPER_TRANSCRIBE_MODEL", "gemini-3.5-transcribe")
GCP_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "reaper-n45div")
LEDGER_BACKEND = os.getenv("REAPER_LEDGER", "sqlite").lower()

# --- Precedent memory (BigQuery, optional) -------------------------------
# OFF unless explicitly enabled: a fresh clone, CI, and every test must never
# reach Google Cloud. "off" makes precedent.recall() a pure no-op.
PRECEDENT_BACKEND = os.getenv("REAPER_PRECEDENT", "off").lower()   # off | bigquery
# The precedent store may live on a different project than the ledger (the
# BigQuery sandbox quota and the Firestore daily quota are separate budgets).
BQ_PROJECT = os.getenv("REAPER_BQ_PROJECT", "") or GCP_PROJECT
BQ_DATASET = os.getenv("REAPER_BQ_DATASET", "reaper_precedents")
BQ_TABLE = os.getenv("REAPER_BQ_TABLE", "precedents")
BQ_LOCATION = os.getenv("REAPER_BQ_LOCATION", "US")
EMBED_MODEL = os.getenv("REAPER_EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = int(os.getenv("REAPER_EMBED_DIM", "768"))
# Must be IDENTICAL at seed time and query time: this is clause-vs-clause
# matching, which is symmetric. Mixing RETRIEVAL_DOCUMENT with RETRIEVAL_QUERY
# would degrade retrieval silently.
EMBED_TASK = os.getenv("REAPER_EMBED_TASK", "SEMANTIC_SIMILARITY")
EMBED_MAX_CHARS = int(os.getenv("REAPER_EMBED_MAX_CHARS", "6000"))
PRECEDENT_TOP_K = int(os.getenv("REAPER_PRECEDENT_TOP_K", "3"))
PRECEDENT_MAX_MATCHES = int(os.getenv("REAPER_PRECEDENT_MAX_MATCHES", "3"))
PRECEDENT_MIN_SIMILARITY = float(os.getenv("REAPER_PRECEDENT_MIN_SIMILARITY", "0.72"))
PRECEDENT_WARN_SIMILARITY = float(os.getenv("REAPER_PRECEDENT_WARN_SIMILARITY", "0.80"))
PRECEDENT_TIMEOUT_S = float(os.getenv("REAPER_PRECEDENT_TIMEOUT_S", "6"))
APP_NAME = "reaper"
