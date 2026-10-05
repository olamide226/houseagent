"""Household time: quiet hours across midnight and recurrence across the 25 Oct 2026 UK clock change."""
from datetime import UTC, date, datetime, time, timedelta

import pytest

from app.core.timeutil import (
    day_bounds,
    in_quiet_hours,
    next_occurrence,
    normalise_rrule,
    occurrences,
    quiet_end,
    to_utc,
)
from tests.helpers import london, wall

LONDON = "Europe/London"
NIGHT = (time(21, 30), time(7, 0))      # the default window, crossing midnight


@pytest.mark.parametrize("at,quiet", [
    ("21:29", False), ("21:30", True), ("23:59", True), ("00:00", True), ("06:59", True), ("07:00", False),
    ("12:00", False),
])
def test_quiet_hours_across_midnight(at, quiet):
    assert in_quiet_hours(time.fromisoformat(at), *NIGHT) is quiet


@pytest.mark.parametrize("at,quiet", [("12:59", False), ("13:00", True), ("14:59", True), ("15:00", False),
                                      ("02:00", False)])
def test_quiet_hours_within_one_day(at, quiet):
    assert in_quiet_hours(time.fromisoformat(at), time(13, 0), time(15, 0)) is quiet


def test_no_window_or_an_empty_window_is_never_quiet():
    assert not in_quiet_hours(time(3, 0), None, None)
    assert not in_quiet_hours(time(3, 0), time(22, 0), None)
    assert not in_quiet_hours(time(3, 0), time(7, 0), time(7, 0))


def test_quiet_end_is_the_next_morning_before_midnight_and_the_same_morning_after():
    assert quiet_end(london("2026-10-06 12:00"), LONDON, *NIGHT) is None
    assert quiet_end(london("2026-10-06 22:15"), LONDON, *NIGHT) == london("2026-10-07 07:00")
    assert quiet_end(london("2026-10-07 02:15"), LONDON, *NIGHT) == london("2026-10-07 07:00")
    assert quiet_end(london("2026-10-06 21:30"), LONDON, *NIGHT) == london("2026-10-07 07:00")
    assert quiet_end(london("2026-10-07 07:00"), LONDON, *NIGHT) is None


def test_quiet_end_keeps_wall_clock_time_over_the_clock_change():
    """The night of 24 to 25 Oct is an hour longer; quiet hours still end at 07:00 local."""
    end = quiet_end(london("2026-10-24 23:00"), LONDON, *NIGHT)
    assert wall(end) == "Sun 25 Oct 07:00" and end == datetime(2026, 10, 25, 7, 0, tzinfo=UTC)
    assert end - london("2026-10-24 23:00") == timedelta(hours=9)       # 8 on any other night


def test_naive_times_are_household_time_and_offsets_are_kept():
    assert to_utc(datetime(2026, 10, 21, 10, 30), LONDON) == datetime(2026, 10, 21, 9, 30, tzinfo=UTC)   # BST
    assert to_utc(datetime(2026, 10, 28, 10, 30), LONDON) == datetime(2026, 10, 28, 10, 30, tzinfo=UTC)  # GMT
    lagos = datetime.fromisoformat("2026-10-28T10:30:00+01:00")
    assert to_utc(lagos, LONDON) == datetime(2026, 10, 28, 9, 30, tzinfo=UTC)


def test_weekly_rule_keeps_its_local_time_across_the_25_october_clock_change():
    """Chatterbox, Tuesdays at 09:00: 08:00 UTC while BST lasts, 09:00 UTC from 27 Oct."""
    start = london("2026-10-06 09:00")
    found = occurrences("FREQ=WEEKLY;BYDAY=TU", start, LONDON, london("2026-10-12 00:00"), london("2026-11-04 00:00"))
    assert [wall(t) for t in found] == ["Tue 13 Oct 09:00", "Tue 20 Oct 09:00", "Tue 27 Oct 09:00", "Tue 3 Nov 09:00"]
    assert [t.hour for t in found] == [8, 8, 9, 9]
    assert found[2] - found[1] == timedelta(days=7, hours=1)


def test_daily_rule_over_the_clock_change_day_and_the_spring_change():
    start = london("2026-10-23 07:30")
    found = occurrences("FREQ=DAILY", start, LONDON, start, london("2026-10-27 00:00"))
    assert [wall(t) for t in found] == ["Fri 23 Oct 07:30", "Sat 24 Oct 07:30", "Sun 25 Oct 07:30", "Mon 26 Oct 07:30"]
    assert [t.hour for t in found] == [6, 6, 7, 7]
    spring = occurrences("FREQ=WEEKLY", london("2027-03-21 09:00"), LONDON, london("2027-03-21 00:00"),
                         london("2027-04-05 00:00"))
    assert [(wall(t), t.hour) for t in spring] == [("Sun 21 Mar 09:00", 9), ("Sun 28 Mar 09:00", 8),
                                                   ("Sun 4 Apr 09:00", 8)]


def test_occurrence_window_includes_its_start_and_excludes_its_end():
    start = london("2026-10-06 09:00")
    assert occurrences("FREQ=WEEKLY", start, LONDON, start, london("2026-10-13 09:00")) == [start]
    assert occurrences("FREQ=WEEKLY", start, LONDON, start + timedelta(seconds=1), london("2026-10-13 09:00")) == []


def test_exception_dates_are_household_local_dates():
    start = london("2026-10-06 09:00")
    skipped = [date(2026, 10, 27)]
    found = occurrences("FREQ=WEEKLY", start, LONDON, london("2026-10-19 00:00"), london("2026-11-04 00:00"), skipped)
    assert [wall(t) for t in found] == ["Tue 20 Oct 09:00", "Tue 3 Nov 09:00"]
    assert wall(next_occurrence("FREQ=WEEKLY", start, LONDON, london("2026-10-20 09:00"), skipped)) == "Tue 3 Nov 09:00"
    assert next_occurrence("FREQ=WEEKLY;UNTIL=20261020T080000Z", start, LONDON, london("2026-10-20 09:00")) is None


def test_rules_are_validated_and_until_is_put_in_utc():
    start = london("2026-10-06 09:00")
    assert normalise_rrule("rrule:freq=weekly;byday=tu", start, LONDON) == "FREQ=WEEKLY;BYDAY=TU"
    # A date-only UNTIL means the end of that household-local day: 23:59:59 BST is 22:59:59 UTC.
    assert normalise_rrule("FREQ=WEEKLY;UNTIL=20261020", start, LONDON) == "FREQ=WEEKLY;UNTIL=20261020T225959Z"
    assert normalise_rrule("FREQ=WEEKLY;UNTIL=20261215T090000Z", start, LONDON) == "FREQ=WEEKLY;UNTIL=20261215T090000Z"
    for bad in ["", "every tuesday", "FREQ=MINUTELY", "FREQ=WEEKLY;BYDAY=XX", "FREQ=WEEKLY;UNTIL=soon", "BYDAY=TU"]:
        with pytest.raises(ValueError):
            normalise_rrule(bad, start, LONDON)


def test_the_clock_change_day_is_25_hours_long():
    start, end = day_bounds(date(2026, 10, 25), LONDON)
    assert end - start == timedelta(hours=25)
    assert day_bounds(date(2026, 10, 26), LONDON)[1] - day_bounds(date(2026, 10, 26), LONDON)[0] == timedelta(hours=24)
