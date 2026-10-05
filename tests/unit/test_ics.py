"""The read-only ICS feed: token access, one VEVENT per active event, recurrence passed through."""
import hashlib
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

from app.agent.tools import run_tool
from app.db import fetch_one, tx
from app.ics.routes import _fold
from app.services import households
from tests.helpers import add_member, ctx_for, london, seed_home, wall

NOW = london("2026-10-05 12:00")


async def seeded():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_member(conn, home, "Tobi", role="child")
        ctx = ctx_for(conn, home, now=NOW)
        for args in [
            {"title": "Chatterbox", "starts_at": "2026-10-06T09:00", "ends_at": "2026-10-06T10:00",
             "rrule": "FREQ=WEEKLY;BYDAY=TU", "participants": ["Tobi"], "location": "library"},
            {"title": "Dinner, at Ada's; bring wine", "starts_at": "2026-10-09T19:00",
             "notes": "Flat 2\nring twice", "location": "12 High St, London"},
            {"title": "Cancelled thing", "starts_at": "2026-10-10T10:00"},
        ]:
            result, is_error = await run_tool("schedule_event", args, ctx)
            assert not is_error, result
        for args in [{"event": "Chatterbox", "occurrence": "2026-10-13", "cancel": True},
                     {"event": "Cancelled thing", "cancel": True}]:
            assert not (await run_tool("modify_event", args, ctx))[1]
        token = await households.new_calendar_token(conn, home.id)
    return home, token


def unfolded(body: str) -> list[str]:
    return body.replace("\r\n ", "").split("\r\n")


def vevents(body: str) -> list[dict[str, str]]:
    events, current = [], None
    for line in unfolded(body):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT":
            events.append(current)
            current = None
        elif current is not None:
            name, _, value = line.partition(":")
            current[name] = value
    return events


async def test_the_feed_needs_the_household_token_and_only_its_hash_is_stored(client):
    home, token = await seeded()
    assert (await client.get("/ics/not-the-token.ics")).status_code == 404
    assert (await client.get(f"/ics/{token}")).status_code == 404
    response = await client.get(f"/ics/{token}.ics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/calendar")
    assert "max-age=900" in response.headers["cache-control"]
    async with tx() as conn:
        stored = await fetch_one(conn, "select calendar_token_hash from households where id = :h", h=home.id)
        assert stored == {"calendar_token_hash": hashlib.sha256(token.encode()).hexdigest()}
        fresh = await households.new_calendar_token(conn, home.id)
    assert (await client.get(f"/ics/{token}.ics")).status_code == 404        # rotated: the old link is dead
    assert (await client.get(f"/ics/{fresh}.ics")).status_code == 200


async def test_the_feed_has_one_event_per_active_event_with_zoned_times_and_escaped_text(client):
    home, token = await seeded()
    body = (await client.get(f"/ics/{token}.ics")).text
    assert body.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n") and body.endswith("END:VCALENDAR\r\n")
    assert all(len(line.encode()) <= 75 for line in body.split("\r\n"))

    chatterbox, dinner = vevents(body)                 # the cancelled event is not in the feed
    async with tx() as conn:
        series = await fetch_one(conn, "select id from events where title = 'Chatterbox'")
    assert chatterbox["UID"] == f"{series['id']}@household-agent"
    assert chatterbox["DTSTART;TZID=Europe/London"] == "20261006T090000"
    assert chatterbox["DTEND;TZID=Europe/London"] == "20261006T100000"
    assert chatterbox["RRULE"] == "FREQ=WEEKLY;BYDAY=TU"
    assert chatterbox["EXDATE;TZID=Europe/London"] == "20261013T090000"
    assert (chatterbox["SUMMARY"], chatterbox["LOCATION"]) == ("Chatterbox for Tobi", "library")

    assert dinner["DTSTART;TZID=Europe/London"] == "20261009T190000"
    assert "RRULE" not in dinner and "DTEND;TZID=Europe/London" not in dinner
    assert dinner["SUMMARY"] == "Dinner\\, at Ada's\\; bring wine"
    assert dinner["LOCATION"] == "12 High St\\, London" and dinner["DESCRIPTION"] == "Flat 2\\nring twice"
    assert home.id


async def test_a_calendar_reading_the_feed_gets_the_right_occurrences_across_the_clock_change(client):
    """Expand the feed's own DTSTART, RRULE and EXDATE the way a calendar client would."""
    _, token = await seeded()
    (chatterbox, _) = vevents((await client.get(f"/ics/{token}.ics")).text)
    text = "\n".join(f"{name}:{chatterbox[name]}" for name in
                     ("DTSTART;TZID=Europe/London", "RRULE", "EXDATE;TZID=Europe/London"))
    series = rrulestr(text, forceset=True, tzids=ZoneInfo)
    found = series.between(london("2026-10-05 00:00"), london("2026-11-04 00:00"))
    assert [wall(t) for t in found] == ["Tue 6 Oct 09:00", "Tue 20 Oct 09:00", "Tue 27 Oct 09:00", "Tue 3 Nov 09:00"]
    assert [t.utcoffset().total_seconds() for t in found] == [3600, 3600, 0, 0]


def test_long_lines_fold_at_75_octets_without_splitting_a_character():
    line = "SUMMARY:" + "Sàlàdé " * 30
    folded = _fold(line)
    assert all(len(part.encode()) <= 75 for part in folded.split("\r\n"))
    assert folded.replace("\r\n ", "") == line
    assert _fold("SUMMARY:short") == "SUMMARY:short"
