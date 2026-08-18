"""The ledger's calendar: real time plus a persistent simulated offset.

The offset lives in the ledger database so simulated time survives process
kills — essential, because the chaos-kill demo must come back up on the same
ledger date it died on. In production the offset stays at zero and the clock
is just the real calendar.
"""

from datetime import date, timedelta

from . import ledger


def offset_days() -> int:
    raw = ledger.get_meta("clock_offset_days")
    return int(raw) if raw else 0


def today() -> date:
    return date.today() + timedelta(days=offset_days())


def advance(days: int) -> date:
    ledger.set_meta("clock_offset_days", str(offset_days() + max(0, days)))
    return today()


def reset() -> None:
    ledger.set_meta("clock_offset_days", "0")
