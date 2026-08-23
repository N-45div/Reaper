import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

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
