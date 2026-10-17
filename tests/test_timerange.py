"""timerange.py: the since / until a tool is given, as moments."""

from datetime import datetime, timedelta, timezone

import pytest

from viwoods_mcp.timerange import TimeRange, parse

PLUS2 = timezone(timedelta(hours=2))
NOW = datetime(2026, 10, 17, 15, 30, tzinfo=PLUS2)


def at(y, mo, d, h=0, mi=0, tz=PLUS2) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=tz).timestamp()


def test_today_and_yesterday_are_whole_days_in_the_pcs_time_zone():
    assert parse("today", now=NOW) == at(2026, 10, 17)
    assert parse("today", end=True, now=NOW) == at(2026, 10, 18)
    assert parse("Yesterday", now=NOW) == at(2026, 10, 16)
    assert parse("yesterday", end=True, now=NOW) == at(2026, 10, 17)


def test_how_long_ago_counts_back_from_now():
    assert parse("12h", now=NOW) == NOW.timestamp() - 12 * 3600
    assert parse("7d", now=NOW) == at(2026, 10, 10, 15, 30)
    assert parse("2 w", now=NOW) == at(2026, 10, 3, 15, 30)
    assert parse("3m", now=NOW) == NOW.timestamp() - 90 * 86400
    assert parse("7d", end=True, now=NOW) == parse("7d", now=NOW)             # an instant either way


def test_a_bare_date_is_a_whole_day_and_a_date_with_a_time_is_that_moment():
    local = lambda *a: datetime(*a).astimezone().timestamp()
    assert parse("2026-10-01") == local(2026, 10, 1)
    assert parse("2026-10-01", end=True) == local(2026, 10, 2)                # through the end of the day
    assert parse("2026-10-01T09:30") == parse("2026-10-01T09:30", end=True) == local(2026, 10, 1, 9, 30)
    assert parse("2026-10-01T09:30+00:00") == at(2026, 10, 1, 9, 30, timezone.utc)   # a stated offset wins


def test_nothing_is_none_and_nonsense_says_what_is_understood():
    assert parse("") is None and parse("   ") is None and parse(None) is None
    for bad in ("last tuesday", "7", "10/01/2026", "2026-13-40", "5y"):
        with pytest.raises(ValueError, match="can't read"):
            parse(bad)


def test_a_range_includes_since_excludes_until_and_must_run_forward():
    rng = TimeRange.of("2026-10-01", "2026-10-03")
    assert rng and at(2026, 10, 1, tz=datetime.now().astimezone().tzinfo) in rng
    assert datetime(2026, 10, 3, 23, 59).astimezone().timestamp() in rng          # through the end of the 3rd
    assert datetime(2026, 10, 4).astimezone().timestamp() not in rng
    assert datetime(2026, 9, 30, 23, 59).astimezone().timestamp() not in rng
    assert not TimeRange.of() and 5.0 in TimeRange.of()
    assert 1e12 in TimeRange.of(since="2026-10-01") and 5.0 not in TimeRange.of(since="2026-10-01")
    assert 5.0 in TimeRange.of(until="2026-10-01") and 1e12 not in TimeRange.of(until="2026-10-01")
    with pytest.raises(ValueError, match="not before"):
        TimeRange.of("2026-10-05", "2026-10-01")
