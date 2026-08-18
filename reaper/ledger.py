"""Ledger facade: one evidence-chain interface, two backends.

REAPER_LEDGER=sqlite    local file (default for dev/tests)
REAPER_LEDGER=firestore the chain lives in Google Cloud Firestore
"""

from .config import LEDGER_BACKEND  # loads .env before the backend choice

if LEDGER_BACKEND == "firestore":
    from .ledger_firestore import *  # noqa: F401,F403
else:
    from .ledger_sqlite import *  # noqa: F401,F403
