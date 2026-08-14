import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

DB_URL = os.getenv("REAPER_DB_URL", f"sqlite+aiosqlite:///{(DATA_DIR / 'reaper.db').as_posix()}")
MODEL = os.getenv("REAPER_MODEL", "gemini-3.5-flash")
APP_NAME = "reaper"
