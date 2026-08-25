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
GCP_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "reaper-n45div")
LEDGER_BACKEND = os.getenv("REAPER_LEDGER", "sqlite").lower()
APP_NAME = "reaper"
