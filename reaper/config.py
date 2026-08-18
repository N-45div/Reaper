import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

DB_URL = os.getenv("REAPER_DB_URL", f"sqlite+aiosqlite:///{(DATA_DIR / 'reaper.db').as_posix()}")
MODEL = os.getenv("REAPER_MODEL", "gemini-3.5-flash")
TRIAGE_MODEL = os.getenv("REAPER_TRIAGE_MODEL", "gemma-4-26b-a4b-it")
GCP_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "reaper-n45div")
LEDGER_BACKEND = os.getenv("REAPER_LEDGER", "sqlite").lower()
APP_NAME = "reaper"
