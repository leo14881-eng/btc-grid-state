"""Shared execution gate; session hours come from the exchange calendar."""
from datetime import time
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

def session_allows_trade(ts, session):
    """Fail closed for missing/malformed evidence and use the event's exact time."""
    try:
        if ts.tzinfo is None or ts.utcoffset() is None:
            return False
        local = ts.astimezone(NY)
        if local.weekday() >= 5 or not isinstance(session, dict):
            return False
        opened, closed = session.get("open"), session.get("close")
        return bool(session.get("date") == local.date().isoformat()
                    and isinstance(opened, time) and isinstance(closed, time)
                    and opened < closed and opened <= local.time() < closed)
    except (TypeError, ValueError, AttributeError):
        return False
