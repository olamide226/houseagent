"""Household time. Timestamps are UTC everywhere; conversion happens only at the edges."""
from collections.abc import Iterable
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

FREQUENCIES = ("HOURLY", "DAILY", "WEEKLY", "MONTHLY", "YEARLY")


def utcnow() -> datetime:
    return datetime.now(UTC)


def local(moment: datetime, timezone: str) -> datetime:
    return moment.astimezone(ZoneInfo(timezone))


def to_utc(moment: datetime, timezone: str) -> datetime:
    """A naive time is household wall-clock time; a time with an offset keeps it."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(timezone))
    return moment.astimezone(UTC)


def day_bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    """The UTC start and end of a household-local calendar day (23 or 25 hours on clock-change days)."""
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start.astimezone(UTC), end.astimezone(UTC)


# ---------------------------------------------------------------- quiet hours
def in_quiet_hours(at: time, start: time | None, end: time | None) -> bool:
    """`start > end` is a window that crosses midnight, e.g. 21:30 to 07:00."""
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= at < end
    return at >= start or at < end


def quiet_end(moment: datetime, timezone: str, start: time | None, end: time | None) -> datetime | None:
    """When the quiet window containing `moment` ends, in UTC. None outside quiet hours."""
    here = local(moment, timezone)
    at = here.time().replace(tzinfo=None)
    if start is None or end is None or not in_quiet_hours(at, start, end):
        return None
    day = here.date() if at < end else here.date() + timedelta(days=1)
    return datetime.combine(day, end, tzinfo=here.tzinfo).astimezone(UTC)


# ---------------------------------------------------------------- recurrence
def parse_rrule(rule: str, dtstart: datetime, timezone: str) -> Any:
    """An RFC 5545 rule anchored at a time-zone-aware start in household time, so occurrences
    keep their wall-clock time when the clocks change."""
    return rrulestr(rule, dtstart=local(dtstart, timezone))


def normalise_rrule(rule: str, dtstart: datetime, timezone: str) -> str:
    """Validate a rule and put UNTIL in UTC, which RFC 5545 requires beside a zoned DTSTART.

    Raises ValueError for anything that cannot be expanded."""
    parts = dict(part.split("=", 1) for part in rule.strip().upper().removeprefix("RRULE:").split(";") if part)
    if parts.get("FREQ") not in FREQUENCIES:
        raise ValueError(f"FREQ must be one of {', '.join(FREQUENCIES)}")
    until = parts.get("UNTIL")
    if until and not until.endswith("Z"):
        ends = (datetime.strptime(until, "%Y%m%dT%H%M%S") if "T" in until
                else datetime.combine(datetime.strptime(until, "%Y%m%d"), time(23, 59, 59)))
        parts["UNTIL"] = f"{to_utc(ends, timezone):%Y%m%dT%H%M%SZ}"
    rule = ";".join(f"{name}={value}" for name, value in parts.items())
    parse_rrule(rule, dtstart, timezone)
    return rule


def occurrences(rule: str, dtstart: datetime, timezone: str, after: datetime, before: datetime,
                exdates: Iterable[date] = ()) -> list[datetime]:
    """Occurrences with after <= t < before, in UTC, without the skipped household-local dates."""
    skipped = set(exdates)
    found = parse_rrule(rule, dtstart, timezone).between(local(after, timezone), local(before, timezone), inc=True)
    return [t.astimezone(UTC) for t in found if t.date() not in skipped and t < before]


def next_occurrence(rule: str, dtstart: datetime, timezone: str, after: datetime,
                    exdates: Iterable[date] = ()) -> datetime | None:
    """The first occurrence strictly after `after`, in UTC."""
    skipped = set(exdates)
    series = parse_rrule(rule, dtstart, timezone)
    found = series.after(local(after, timezone))
    while found is not None and found.date() in skipped:
        found = series.after(found)
    return None if found is None else found.astimezone(UTC)
