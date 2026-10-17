"""`since` / `until` as the tools take them: a date, a date and time, or how long ago.

    2026-10-01            a date: the whole day (as `since` its start, as `until` through its end)
    2026-10-01T09:30      a date and time, in the PC's time zone unless it names an offset
    today, yesterday      those days
    7d  2w  12h  3m       that long ago ("m" is months of 30 days; there is no minutes)
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

AGO = re.compile(r"^(\d+)\s*([hdwm])$")
UNITS = {"h": timedelta(hours=1), "d": timedelta(days=1), "w": timedelta(weeks=1),
         "m": timedelta(days=30)}


def parse(text: str, end: bool = False, now: datetime | None = None) -> float | None:
    """`text` as epoch seconds; None for nothing. With `end`, a date means the moment its day is over."""
    text = (text or "").strip().lower()
    if not text:
        return None
    now = now or datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day = timedelta(days=1)
    if text in ("today", "yesterday"):
        start = midnight - (day if text == "yesterday" else timedelta())
        return (start + day if end else start).timestamp()
    ago = AGO.match(text)
    if ago:
        return (now - int(ago.group(1)) * UNITS[ago.group(2)]).timestamp()
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"I can't read “{text}” as a time: use a date like 2026-10-01, a date and "
                         "time like 2026-10-01T09:30, today, yesterday, or how long ago (7d, 2w, 12h, 3m)") from None
    if when.tzinfo is None:
        when = when.astimezone()                       # the PC's own time zone
    if end and len(text) <= 10:                        # a bare date: through the end of that day
        when = when.replace(hour=0, minute=0, second=0, microsecond=0) + day
    return when.timestamp()


@dataclass(frozen=True)
class TimeRange:
    since: float | None = None
    until: float | None = None

    @classmethod
    def of(cls, since: str = "", until: str = "", now: datetime | None = None) -> "TimeRange":
        rng = cls(parse(since, False, now), parse(until, True, now))
        if rng.since is not None and rng.until is not None and rng.since >= rng.until:
            raise ValueError("`since` is not before `until`")
        return rng

    def __bool__(self) -> bool:
        return self.since is not None or self.until is not None

    def __contains__(self, ts: float) -> bool:
        return (self.since is None or ts >= self.since) and (self.until is None or ts < self.until)
