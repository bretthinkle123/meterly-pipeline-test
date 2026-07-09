"""Hour-window flooring shared by the events and usage services.

`date_trunc('hour', <timestamptz>)` is STABLE (not IMMUTABLE) in PostgreSQL —
its result depends on the session `TimeZone` — so it cannot back a stored
generated column. Flooring explicitly in UTC here sidesteps that timezone
trap entirely and is directly unit-testable.
"""

from datetime import datetime, timezone


def floor_to_hour_utc(timestamp: datetime) -> datetime:
    """Return `timestamp` floored to the start of its UTC hour.

    Converts to UTC first if `timestamp` carries a different timezone, so the
    result is always a UTC-normalized hour boundary regardless of the input's
    original offset.
    """
    as_utc = timestamp.astimezone(timezone.utc)
    return as_utc.replace(minute=0, second=0, microsecond=0)
