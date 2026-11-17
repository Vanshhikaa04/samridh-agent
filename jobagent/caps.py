"""Daily caps count only what happened after the last reset (data/cap_reset.txt, UTC). Reset: python -m jobagent reset-caps"""
from datetime import datetime, timezone

from .config import DATA

MARK = DATA / "cap_reset.txt"


def since() -> str:
    """UTC 'YYYY-MM-DD HH:MM:SS' of the last reset, or '' if never."""
    try:
        return MARK.read_text().strip()
    except Exception:
        return ""


def reset() -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    MARK.write_text(now)
    import json
    from . import lilogin
    s = lilogin._state()  # LinkedIn lookups + Easy Apply counters
    s.update(count=0, easy_count=0)
    lilogin.STATE.write_text(json.dumps(s))
    return now
