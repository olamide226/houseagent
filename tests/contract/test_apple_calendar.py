"""Apple's iCloud Calendar over CalDAV: what is read from it and what is sent to it (ADR 0041).

No Apple account existed when this was built, so nothing here has been run against iCloud. There
are three kinds of test, and each says which it is:

- Against Radicale (the `icloud` fixture, tests/caldav.py): a real CalDAV server on this machine.
  Requests, etags, sync tokens and stored files are real. Radicale is not iCloud.
- iCloud's answers as published: `principal.xml`, `home.xml`, `calendars.xml` and `files.xml` in
  fixtures/apple_calendar are copied from the python-caldav project's tests, which say they are
  "real XML received from" iCloud (tests/test_caldav_unit.py there). `changes.xml` is written by
  hand from RFC 6578 and one published report of what iCloud adds to it; no capture of that
  answer from iCloud was found anywhere.
- Files as a calendar app writes them: `series_with_a_moved_date.ics` is written by hand in the
  form Apple's Calendar app uses. It is not a recording.
"""
import base64
from dataclasses import replace
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from app.calendars import apple
from app.calendars.base import AuthLost, Entry, Gone, Refused, Remote, SyncReset, Unavailable
from tests.caldav import PASSWORDS, REPLACEMENT, vevent
from tests.helpers import london, wall

FIXTURES = Path(__file__).parent / "fixtures" / "apple_calendar"
NOW = london("2026-10-05 12:00")
OLA = "ola@example.test"
EVENT_ID = "1b671a64-40d5-491e-99b0-da01ff1f3341"
ICLOUD_HOME = "https://p62-caldav.icloud.com:443/17149682/calendars/"
ICLOUD_CALENDAR = f"{ICLOUD_HOME}testcalendar-84439d0b-ce46-4416-b978-7b4009122c64/"
PARTY = "/17149682/calendars/testcalendar-84439d0b-ce46-4416-b978-7b4009122c64/20010712T182145Z-123401%40example.com.ics"
SIGN_IN = "Basic " + base64.b64encode(b"ada@example.test:made-up-pass").decode()


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def entry(summary="Dentist", start="2026-10-09 14:00", end="2026-10-09 15:00", **more) -> Entry:
    return Entry(summary=summary, start=london(start), end=london(end), **more)


def on_icloud(calendar_url: str = ICLOUD_CALENDAR) -> apple.AppleCalendar:
    return apple.AppleCalendar(apple.SERVER, "ada@example.test", "made-up-pass", calendar_url, "Europe/London",
                               clock=lambda: NOW)


@pytest.fixture
def phone(icloud):
    return icloud.phone(OLA)


@pytest.fixture
async def calendar(icloud, phone):
    async with apple.AppleCalendar(icloud.url, OLA, PASSWORDS[OLA], phone.folder, "Europe/London",
                                   clock=lambda: NOW) as connected:
        yield connected


# ---------------------------------------------------------------- iCloud's answers, as published
@respx.mock
async def test_the_calendars_are_found_through_icloud_s_own_answers_and_the_password_goes_only_to_apple():
    root = respx.route(method="PROPFIND", url="https://caldav.icloud.com/").respond(207, text=fixture("principal.xml"))
    principal = respx.route(method="PROPFIND", url="https://caldav.icloud.com/17149682/principal/").respond(
        207, text=fixture("home.xml"))
    home = respx.route(method="PROPFIND", url=ICLOUD_HOME).respond(207, text=fixture("calendars.xml"))

    found = await apple.calendars(apple.SERVER, "ada@example.test", "made-up-pass")
    assert [(choice.id, choice.name) for choice in found] == [       # not the folder itself, and not the inbox
        (f"{ICLOUD_HOME}06888b87-397f-11eb-943b-3af9d3928d42/", "calfoo3"),
        (f"{ICLOUD_HOME}testcalendar-e2910e0a-feab-4b51-b3a8-55828acaa912/", "Yep")]
    asked = [route.calls.last.request for route in (root, principal, home)]
    assert [request.headers["depth"] for request in asked] == ["0", "0", "1"]
    assert all(request.headers["authorization"] == SIGN_IN for request in asked)
    assert b"current-user-principal" in asked[0].content and b"calendar-home-set" in asked[1].content

    # An answer that points away from Apple is not followed, so the password is not sent there.
    elsewhere = respx.route(host="calendars.example.test").respond(207, text=fixture("calendars.xml"))
    principal.respond(207, text=fixture("home.xml").replace("p62-caldav.icloud.com:443", "calendars.example.test"))
    with pytest.raises(Refused):
        await apple.calendars(apple.SERVER, "ada@example.test", "made-up-pass")
    principal.respond(207, text=fixture("home.xml").replace("https://p62", "http://p62"))     # nor without TLS
    with pytest.raises(Refused):
        await apple.calendars(apple.SERVER, "ada@example.test", "made-up-pass")
    assert elsewhere.call_count == 0


@respx.mock
async def test_a_refused_sign_in_is_tried_once_more_and_then_called_lost_and_a_busy_icloud_is_tried_later():
    # What iCloud answers to a sign-in it does not accept, as seen on 10 October 2026: 401, no body.
    root = respx.route(method="PROPFIND", url="https://caldav.icloud.com/").respond(
        401, headers={"WWW-Authenticate": 'Basic realm="MMCalDav"'})
    with pytest.raises(AuthLost) as lost:
        await apple.calendars(apple.SERVER, "ada@example.test", "made-up-pass")
    # Asked once: a person is at the form, and Apple counts wrong passwords. The worker asks twice (below).
    assert root.call_count == 1 and "made-up-pass" not in str(lost.value) and "ada@" not in str(lost.value)
    for status in (503, 429, 500):         # 503 is what people report for iCloud's rate limit
        root.respond(status, text="Rate Limit Exceeded")
        with pytest.raises(Unavailable):
            await apple.calendars(apple.SERVER, "ada@example.test", "made-up-pass")
    root.mock(side_effect=httpx.ConnectTimeout("timed out"))
    with pytest.raises(Unavailable):
        await apple.calendars(apple.SERVER, "ada@example.test", "made-up-pass")
    root.respond(207, text='<multistatus xmlns="DAV:"/>')           # an answer with no calendars in it
    with pytest.raises(Refused):
        await apple.calendars(apple.SERVER, "ada@example.test", "made-up-pass")


@respx.mock
async def test_a_first_read_lists_every_file_then_fetches_them_and_icloud_s_extras_are_left_out():
    reports = respx.route(method="REPORT", url=ICLOUD_CALENDAR).mock(side_effect=[
        httpx.Response(207, text=fixture("changes.xml")), httpx.Response(207, text=fixture("files.xml"))])
    async with on_icloud() as calendar:
        found, token = await calendar.changes(None, london("2006-07-01 12:00"),
                                              followed=[PARTY, PARTY.replace("20010712T182145Z-123401%40example.com",
                                                                             "no-longer-there")])

    listing, fetch = (call.request for call in reports.calls)
    assert listing.headers["depth"] == "0" and "depth" not in fetch.headers        # RFC 6578 3.2, RFC 4791 7.9
    assert b"<d:sync-token></d:sync-token><d:sync-level>1</d:sync-level>" in listing.content
    assert fetch.content.count(b"<d:href>") == 1 and PARTY.encode() in fetch.content   # not the folder itself
    assert token == "place-after-first-read"
    party, *gone = found
    assert (party.id, party.etag, party.owner) == (PARTY, '"etag-of-the-party"', None)
    assert party.entry == Entry(summary="Bastille Day Party", start=london("2006-07-14 18:00"),
                                end=london("2006-07-15 05:00"))
    # What the list says went, and what is followed here and was not in a list of everything.
    assert sorted((remote.id.rpartition("/")[2], remote.deleted) for remote in gone) == [
        ("deleted-on-the-phone.ics", True), ("no-longer-there.ics", True)]


@respx.mock
async def test_a_list_that_comes_in_parts_is_followed_and_a_forgotten_place_says_start_again():
    folder = ICLOUD_CALENDAR.removeprefix("https://p62-caldav.icloud.com:443")

    def part(names: list[str], token: str, more: bool) -> httpx.Response:
        files = "".join(f"<response><href>{folder}{name}.ics</href><status>HTTP/1.1 404 Not Found</status></response>"
                        for name in names)
        full = (f"<response><href>{folder}</href><status>HTTP/1.1 507 Insufficient Storage</status></response>"
                if more else "")
        return httpx.Response(207, text=f'<multistatus xmlns="DAV:">{files}{full}<sync-token>{token}</sync-token>'
                                        "</multistatus>")

    reports = respx.route(method="REPORT", url=ICLOUD_CALENDAR).mock(side_effect=[
        part(["a", "b"], "place-2", True), part(["c"], "place-3", False)])
    async with on_icloud() as calendar:
        found, token = await calendar.changes("place-1", NOW)
        assert ([remote.id.rpartition("/")[2] for remote in found], token) == (["a.ics", "b.ics", "c.ics"], "place-3")
        assert [b"place-1" in call.request.content for call in reports.calls] == [True, False]
        assert b"<d:sync-token>place-2</d:sync-token>" in reports.calls[1].request.content

        # RFC 6578: 403 and `valid-sync-token`. What iCloud really answers is not known.
        reports.mock(side_effect=None)
        reports.respond(403, text='<error xmlns="DAV:"><valid-sync-token/></error>')
        with pytest.raises(SyncReset):
            await calendar.changes("place-3", NOW)
        reports.respond(403)
        with pytest.raises(Refused):                 # with no place to forget, a refusal is a refusal
            await calendar.changes(None, NOW)
        reports.respond(404)
        with pytest.raises(Gone):                    # the calendar itself was deleted
            await calendar.changes(None, NOW)
        reports.respond(401)
        asked = reports.call_count
        with pytest.raises(AuthLost):
            await calendar.changes("place-3", NOW)
        assert reports.call_count == asked + 2      # once more, before a connection is stopped for its owner to mend


@respx.mock
async def test_a_file_that_is_listed_but_not_handed_over_is_asked_for_by_itself_and_never_holds_the_rest_back():
    listing, nothing = httpx.Response(207, text=fixture("changes.xml")), httpx.Response(207, text='<multistatus xmlns="DAV:"/>')
    reports = respx.route(method="REPORT", url=ICLOUD_CALENDAR)
    party = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:p\r\nDTSTART:20261014T170000Z\r\nSUMMARY:Party\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    got = respx.route(method="GET", url=f"https://p62-caldav.icloud.com{PARTY}")
    async with on_icloud() as calendar:
        for answer, expected in [
            (httpx.Response(200, text=party, headers={"ETag": '"by-itself"'}), ("Party", '"by-itself"', None)),
            (httpx.Response(403), (None, None, "it could not be read")),       # left out; the others still come
        ]:
            reports.mock(side_effect=[listing, nothing])
            got.mock(return_value=answer)
            found, _ = await calendar.changes(None, NOW)
            (read,) = [remote for remote in found if not remote.deleted]
            assert (read.entry and read.entry.summary, read.etag and expected[1], read.skipped) == expected
        reports.mock(side_effect=[listing, nothing])
        got.mock(return_value=httpx.Response(404))                              # deleted in the meantime
        found, _ = await calendar.changes(None, NOW)
        assert sorted(remote.id.rpartition("/")[2] for remote in found if remote.deleted) == [
            "20010712T182145Z-123401%40example.com.ics", "deleted-on-the-phone.ics"]
        reports.mock(side_effect=[listing, nothing])
        got.mock(return_value=httpx.Response(503))
        with pytest.raises(Unavailable):                                        # the whole read is tried again
            await calendar.changes(None, NOW)


def test_an_answer_that_is_not_plain_xml_is_not_read():
    with pytest.raises(Unavailable):
        apple._answers(b'<!DOCTYPE m [<!ENTITY a "aaaa">]><multistatus xmlns="DAV:"><response><href>&a;</href></response></multistatus>')
    with pytest.raises(Unavailable):
        apple._answers(b"<html>Service unavailable")


@respx.mock
async def test_when_a_write_is_answered_without_an_etag_it_is_asked_for():
    path = f"/17149682/calendars/testcalendar-84439d0b-ce46-4416-b978-7b4009122c64/houseagent-{EVENT_ID}-"
    put = respx.route(method="PUT", url__startswith=ICLOUD_CALENDAR.replace(":443", "")).respond(201)
    asked = respx.route(method="PROPFIND", url__startswith=ICLOUD_CALENDAR.replace(":443", "")).respond(
        207, text=f'<multistatus xmlns="DAV:"><response><href>{path}x.ics</href><propstat><prop><getetag>"C=4@U=x"'
                  "</getetag></prop><status>HTTP/1.1 200 OK</status></propstat></response></multistatus>")
    async with on_icloud() as calendar:
        remote_id, etag = await calendar.create(entry(), EVENT_ID)
        assert remote_id.startswith(path) and remote_id.endswith(".ics") and etag == '"C=4@U=x"'
        assert put.calls.last.request.headers["if-none-match"] == "*" and asked.call_count == 1
        assert put.calls.last.request.headers["content-type"] == "text/calendar; charset=utf-8"
        asked.respond(503)
        assert (await calendar.create(entry(), EVENT_ID))[1] is None      # it was made; its echo is told by content
        put.respond(507)
        with pytest.raises(Refused):                                       # no room left in the account
            await calendar.create(entry(), EVENT_ID)
        put.respond(403)
        with pytest.raises(Refused):                                       # a calendar that can only be looked at
            await calendar.create(entry(), EVENT_ID)


# ---------------------------------------------------------------- a file a calendar app wrote
def test_a_series_with_one_date_moved_is_read_as_the_series_and_one_more_event():
    series, moved = apple.remotes_of("/c/football.ics", '"7"', fixture("series_with_a_moved_date.ics"), "Europe/London")
    assert (series.id, series.etag, series.owner, series.series_id) == ("/c/football.ics", '"7"', None, None)
    assert series.entry == Entry(
        summary="Football", start=london("2026-09-01 17:00"), end=london("2026-09-01 18:00"),
        rrule="FREQ=WEEKLY;UNTIL=20261215T235959Z", location="Riverside Pitches, 12 Mill Lane",
        exdates=(date(2026, 9, 29), date(2026, 10, 13)))        # the one deleted, and the one moved
    assert (moved.id, moved.etag, moved.owner) == ("/c/football.ics#20261013", '"7"', None)
    assert moved.entry == Entry(summary="Football", start=london("2026-10-14 18:00"), end=london("2026-10-14 19:00"))


def test_a_change_touches_only_what_changed_and_keeps_what_the_calendar_app_put_there():
    was = fixture("series_with_a_moved_date.ics")
    series, _ = apple.remotes_of("/c/football.ics", None, was, "Europe/London")
    later = replace(series.entry, start=london("2026-09-01 17:30"), end=london("2026-09-01 18:30"),
                    exdates=(date(2026, 9, 29), date(2026, 10, 13), date(2026, 10, 27)))
    now = apple.changed_file(was, "", later, {"start", "end", "exdates"}, "Europe/London", NOW)
    lines = now.replace("\r\n ", "").splitlines()

    for kept in ("TRIGGER:-PT30M", "X-WR-ALARMUID:0B7A9C1E-2D3F-4A5B-8C6D-7E8F9A0B1C2D", "TRANSP:OPAQUE",
                 "UID:6E2C1B0A-5D7F-4C3B-9A11-2F8E7D6C5B4A", "LOCATION:Riverside Pitches\\n12 Mill Lane",
                 "RRULE:FREQ=WEEKLY;UNTIL=20261215T235959Z", "CREATED:20260820T081502Z"):
        assert kept in lines
    assert any(line.startswith("X-APPLE-STRUCTURED-LOCATION;") for line in lines)
    assert "DTSTART;TZID=Europe/London:20260901T173000" in lines and "DTEND;TZID=Europe/London:20260901T183000" in lines
    # Skipped dates are at the series' new time, the second after the clocks change; the moved date
    # keeps its own VEVENT and is not written as skipped as well, and it still names its date.
    assert "EXDATE;TZID=Europe/London:20260929T173000,20261027T173000" in lines
    assert "RECURRENCE-ID;TZID=Europe/London:20261013T173000" in lines
    assert "DTSTART;TZID=Europe/London:20261014T180000" in lines             # the moved date itself is as it was
    assert lines.count("DTSTAMP:20261005T110000Z") == 1 and "SEQUENCE:2" in lines     # iCloud's doubled stamp is one
    assert lines.count("BEGIN:VTIMEZONE") == 1 and lines.count("BEGIN:VEVENT") == 2

    again = apple.remotes_of("/c/football.ics", None, now, "Europe/London")
    assert again[0].entry == later and again[1].entry.start == london("2026-10-14 18:00")


def test_the_moved_date_is_changed_in_its_own_event_and_deleting_it_leaves_the_date_skipped():
    was = fixture("series_with_a_moved_date.ics")
    moved = entry("Football", "2026-10-15 18:30", "2026-10-15 19:30", location="Sports hall")
    now = apple.changed_file(was, "20261013", moved, {"start", "end", "location"}, "Europe/London", NOW)
    series, one = apple.remotes_of("/c/f.ics", None, now, "Europe/London")
    assert one.entry == moved and series.entry.start == london("2026-09-01 17:00")
    assert "RECURRENCE-ID;TZID=Europe/London:20261013T170000" in now.splitlines()

    with pytest.raises(Gone):                # that date has no event of its own
        apple.changed_file(was, "20261020", moved, {"start"}, "Europe/London", NOW)
    with pytest.raises(Refused):             # one date cannot become a series
        apple.changed_file(was, "20261013", entry(rrule="FREQ=DAILY"), {"rrule"}, "Europe/London", NOW)

    without = apple.without_date(was, "20261013", "Europe/London", NOW)
    (only,) = apple.remotes_of("/c/f.ics", None, without, "Europe/London")
    assert only.entry.exdates == (date(2026, 9, 29), date(2026, 10, 13))
    assert "EXDATE;TZID=Europe/London:20261013T170000" in without.splitlines()
    with pytest.raises(Gone):
        apple.without_date(without, "20261013", "Europe/London", NOW)


def file(*events: str) -> str:
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n" + "".join(events) + "END:VCALENDAR\r\n"


def read(*lines: str, uid: str = "one") -> Remote:
    (remote,) = apple.remotes_of("/c/one.ics", '"1"', file(vevent(uid, *lines)), "Europe/London")
    return remote


def test_every_way_a_time_is_written_is_read_as_the_same_instant():
    for start in ("DTSTART;TZID=Europe/London:20261009T140000", "DTSTART:20261009T130000Z",
                  "DTSTART:20261009T140000",                               # no zone: household time
                  "DTSTART;TZID=America/New_York:20261009T090000",
                  "DTSTART;TZID=GMT Standard Time:20261009T140000"):       # a zone as Outlook names it
        found = read(start, "SUMMARY:Dentist").entry
        assert (wall(found.start), wall(found.end), found.all_day) == ("Fri 9 Oct 14:00", "Fri 9 Oct 15:00", False)
    assert wall(read("DTSTART:20261009T130000Z", "DURATION:PT90M").entry.end) == "Fri 9 Oct 15:30"
    assert wall(read("DTSTART:20261009T130000Z", "DTEND:20261009T163000Z").entry.end) == "Fri 9 Oct 17:30"

    day = read("DTSTART;VALUE=DATE:20261009", "DTEND;VALUE=DATE:20261010", "SUMMARY:Inset day").entry
    assert day.all_day and (wall(day.start), wall(day.end)) == ("Fri 9 Oct 00:00", "Sat 10 Oct 00:00")
    two = read("DTSTART;VALUE=DATE:20261024", "DURATION:P2D").entry         # over the night the clocks change
    assert (wall(two.start), wall(two.end)) == ("Sat 24 Oct 00:00", "Mon 26 Oct 00:00")
    assert wall(read("DTSTART;VALUE=DATE:20261009").entry.end) == "Sat 10 Oct 00:00"   # a day with no end is one day


def test_words_are_read_as_a_person_wrote_them_and_an_event_with_no_title_has_one():
    found = read("DTSTART:20261009T130000Z", "SUMMARY:Dentist\\, for  Ada\\; again",
                 "LOCATION:Cut & Dry\\n4 High Street", "DESCRIPTION:Ask for Sam\\n\\nBring the form ").entry
    assert (found.summary, found.location, found.notes) == (
        "Dentist, for Ada; again", "Cut & Dry, 4 High Street", "Ask for Sam\nBring the form")
    bare = read("DTSTART:20261009T130000Z").entry
    assert (bare.summary, bare.location, bare.notes) == ("(No title)", None, None)


def test_a_repeat_is_read_with_its_skipped_dates_and_what_cannot_be_followed_is_left_out():
    weekly = read("DTSTART;TZID=Europe/London:20261006T170000", "RRULE:BYDAY=TU;FREQ=WEEKLY;UNTIL=20261215",
                  "EXDATE;TZID=Europe/London:20261013T170000", "EXDATE:20261027T170000Z,20261103T170000Z").entry
    assert weekly.rrule == "FREQ=WEEKLY;BYDAY=TU;UNTIL=20261215T235959Z"       # one spelling, the end as an instant
    assert weekly.exdates == (date(2026, 10, 13), date(2026, 10, 27), date(2026, 11, 3))
    yearly = read("DTSTART;VALUE=DATE:20261009", "RRULE:FREQ=YEARLY", "EXDATE;VALUE=DATE:20271009").entry
    assert yearly.all_day and yearly.exdates == (date(2027, 10, 9),)

    for lines in (("DTSTART:20261009T130000Z", "RRULE:FREQ=WEEKLY", "RRULE:FREQ=MONTHLY"),
                  ("DTSTART:20261009T130000Z", "RRULE:FREQ=WEEKLY", "RDATE:20261011T130000Z"),
                  ("DTSTART:20261009T130000Z", "RDATE:20261011T130000Z")):
        assert read(*lines).skipped == "it repeats in a way that cannot be followed here"
    assert read("DTSTART:20261009T130000Z", "RRULE:FREQ=MINUTELY").skipped.startswith("FREQ must be one of")
    assert read("SUMMARY:No start").skipped == "it could not be read"
    this_and_later = apple.remotes_of("/c/one.ics", None, file(
        vevent("one", "DTSTART:20261006T160000Z", "RRULE:FREQ=WEEKLY"),
        vevent("one", "DTSTART:20261013T170000Z", "RECURRENCE-ID;RANGE=THISANDFUTURE:20261013T160000Z")),
        "Europe/London")
    assert [remote.skipped for remote in this_and_later] == ["it repeats in a way that cannot be followed here"]


def test_what_is_not_one_whole_event_is_left_out_and_a_cancelled_one_is_gone():
    (todo,) = apple.remotes_of("/c/todo.ics", None, file("BEGIN:VTODO\r\nUID:t\r\nSUMMARY:Bins\r\nEND:VTODO\r\n"),
                               "Europe/London")
    assert todo.skipped == "it is not an event" and todo.entry is None
    (part,) = apple.remotes_of("/c/part.ics", None, file(
        vevent("s", "DTSTART:20261013T170000Z", "RECURRENCE-ID:20261013T160000Z")), "Europe/London")
    assert part.skipped == "it is one date of a repeating event that is not on this calendar"
    (junk,) = apple.remotes_of("/c/junk.ics", None, "not a calendar", "Europe/London")
    assert junk.skipped == "it could not be read"
    assert read("DTSTART:20261009T130000Z", "STATUS:CANCELLED").deleted

    series, dropped = apple.remotes_of("/c/s.ics", None, file(
        vevent("s", "DTSTART:20261006T160000Z", "RRULE:FREQ=WEEKLY"),
        vevent("s", "DTSTART:20261013T160000Z", "RECURRENCE-ID:20261013T160000Z", "STATUS:CANCELLED")),
        "Europe/London")
    assert series.entry.exdates == (date(2026, 10, 13),) and (dropped.id, dropped.deleted) == ("/c/s.ics#20261013", True)


def test_an_event_this_app_made_is_known_by_its_uid_and_nothing_else_is():
    ours = read("DTSTART:20261009T130000Z", uid=f"houseagent-{EVENT_ID}-0a1b2c3d")
    assert ours.owner == EVENT_ID
    assert read("DTSTART:20261009T130000Z", uid="6E2C1B0A-5D7F-4C3B-9A11-2F8E7D6C5B4A").owner is None
    # A date of our own series that was changed by itself is nobody's copy: it must not be taken for a second one.
    series, moved = apple.remotes_of("/c/s.ics", None, file(
        vevent(f"houseagent-{EVENT_ID}-0a1b2c3d", "DTSTART:20261006T160000Z", "RRULE:FREQ=WEEKLY"),
        vevent(f"houseagent-{EVENT_ID}-0a1b2c3d", "DTSTART:20261014T160000Z", "RECURRENCE-ID:20261013T160000Z")),
        "Europe/London")
    assert (series.owner, moved.owner) == (EVENT_ID, None)


def test_an_event_with_guests_is_read_but_never_written_so_nobody_is_emailed():
    invited = file(vevent("party", "DTSTART:20261009T180000Z", "SUMMARY:Party",
                          "ORGANIZER;CN=Ola:mailto:ola@example.test",
                          "ATTENDEE;CN=Grace;PARTSTAT=NEEDS-ACTION:mailto:grace@example.test"))
    (party,) = apple.remotes_of("/c/party.ics", None, invited, "Europe/London")
    assert party.entry.summary == "Party"
    with pytest.raises(Refused, match="guests"):
        apple.changed_file(invited, "", entry("Party at ours"), {"summary"}, "Europe/London", NOW)
    with pytest.raises(Refused):
        apple.changed_file("not a calendar", "", entry(), {"summary"}, "Europe/London", NOW)


def test_a_new_event_is_a_whole_file_with_its_zone_defined_and_no_alarm():
    weekly = entry("Chatterbox for Tobi", "2026-10-06 09:00", "2026-10-06 10:00", rrule="FREQ=WEEKLY;BYDAY=TU",
                   exdates=(date(2026, 10, 13), date(2026, 10, 27)), location="Library", notes="Bring a snack\nand a drink")
    made = apple.new_file("houseagent-x", weekly, "Europe/London", NOW)
    lines = made.replace("\r\n ", "").splitlines()
    assert made.endswith("END:VCALENDAR\r\n") and lines[:3] == ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//household-agent//EN"]
    assert lines.count("BEGIN:VTIMEZONE") == 1 and "TZID:Europe/London" in lines       # RFC 5545 3.6.5
    assert not [line for line in lines if "VALARM" in line or line.startswith(("ATTENDEE", "ORGANIZER"))]
    event = lines[lines.index("BEGIN:VEVENT") + 1:lines.index("END:VEVENT")]
    assert sorted(event) == sorted([
        "UID:houseagent-x", "DTSTAMP:20261005T110000Z", "CREATED:20261005T110000Z", "SUMMARY:Chatterbox for Tobi",
        "DTSTART;TZID=Europe/London:20261006T090000", "DTEND;TZID=Europe/London:20261006T100000",
        "RRULE:FREQ=WEEKLY;BYDAY=TU", "EXDATE;TZID=Europe/London:20261013T090000,20261027T090000",
        "LOCATION:Library", "DESCRIPTION:Bring a snack\\nand a drink"])

    day = entry("Inset day", "2026-10-09 00:00", "2026-10-10 00:00", all_day=True,
                rrule="FREQ=YEARLY;UNTIL=20301009T225959Z", exdates=(date(2027, 10, 9),))
    lines = apple.new_file("houseagent-y", day, "Europe/London", NOW).splitlines()
    assert {"DTSTART;VALUE=DATE:20261009", "DTEND;VALUE=DATE:20261010", "RRULE:FREQ=YEARLY;UNTIL=20301009",
            "EXDATE;VALUE=DATE:20271009"} <= set(lines) and "BEGIN:VTIMEZONE" not in lines


# ---------------------------------------------------------------- against a CalDAV server (Radicale)
async def test_the_calendars_offered_are_the_ones_events_can_be_added_to(icloud):
    icloud.phone(OLA, "family", "Family")
    icloud.phone(OLA, "tasks", "Reminders", holds=("VTODO",))      # iCloud keeps reminders in a calendar of their own
    icloud.phone(OLA, "school", "School  run")
    found = await apple.calendars(icloud.url, OLA, PASSWORDS[OLA])
    assert sorted((choice.id, choice.name) for choice in found) == [
        (f"{icloud.url}/ola%40example.test/family/", "Family"), (f"{icloud.url}/ola%40example.test/school/", "School run")]
    with pytest.raises(AuthLost):
        await apple.calendars(icloud.url, OLA, "not-the-password")
    with pytest.raises(AuthLost):
        await apple.calendars(icloud.url, "nobody@example.test", PASSWORDS[OLA])


async def test_what_is_made_is_read_back_the_same_and_making_it_twice_makes_one(calendar, phone):
    nothing, start = await calendar.changes(None, NOW)
    assert nothing == [] and start
    one = entry("Dentist for Ada", location="4 High Street", notes="Ask for Sam\nBring the form; and a pen, too")
    weekly = entry("Football", "2026-10-06 17:00", "2026-10-06 18:00", rrule="FREQ=WEEKLY;BYDAY=TU;UNTIL=20261215T235959Z",
                   exdates=(date(2026, 10, 13), date(2026, 10, 27)))       # the second is after the clocks change
    day = entry("Inset day", "2026-10-09 00:00", "2026-10-10 00:00", all_day=True,
                rrule="FREQ=YEARLY;UNTIL=20301009T225959Z", exdates=(date(2027, 10, 9),))
    made = {}
    for number, sent in enumerate((one, weekly, day)):
        made[sent.summary] = await calendar.create(sent, EVENT_ID[:-1] + str(number))
    again = await calendar.create(one, EVENT_ID[:-1] + "0")                # the answer was lost: asked again
    assert again == made["Dentist for Ada"] and len(phone.files()) == 3
    assert made["Football"][0].startswith("/ola%40example.test/family/houseagent-1b671a64")

    found, token = await calendar.changes(start, NOW)
    assert {remote.entry.summary: (remote.id, remote.etag) for remote in found} == made    # the etag a write returned
    by_title = {remote.entry.summary: remote for remote in found}
    assert (by_title["Dentist for Ada"].entry, by_title["Football"].entry, by_title["Inset day"].entry) == (
        one, weekly, day)
    assert [remote.owner[-1] for remote in sorted(found, key=lambda remote: remote.owner)] == ["0", "1", "2"]
    assert await calendar.changes(token, NOW) == ([], token)               # and then nothing has changed
    assert phone.lines(made["Football"][0], "EXDATE") == ["EXDATE;TZID=Europe/London:20261013T170000,20261027T170000"]


async def test_a_change_sends_only_what_changed_and_keeps_the_phone_s_own_details(calendar, phone):
    theirs = phone.put("hair", vevent(
        "hair", "DTSTART;TZID=Europe/London:20261007T160000", "DTEND;TZID=Europe/London:20261007T164500",
        "SUMMARY:Hairdresser", "LOCATION:Cut & Dry", "DESCRIPTION:Ask for Sam", "X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC",
        "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Reminder", "TRIGGER:-PT30M", "END:VALARM"))
    (before,), token = await calendar.changes(None, NOW)
    assert before.id == theirs and before.entry.location == "Cut & Dry"

    etag = await calendar.update(theirs, entry("Hair", "2026-10-07 16:30", "2026-10-07 17:15"),
                                 {"summary", "start", "end", "location"})
    (after,), token = await calendar.changes(token, NOW)
    assert after.etag == etag != before.etag                               # our own write is known when it comes back
    assert after.entry == entry("Hair", "2026-10-07 16:30", "2026-10-07 17:15", notes="Ask for Sam")
    kept = phone.read(theirs)
    assert "TRIGGER:-PT30M" in kept and "X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC" in kept and "LOCATION" not in kept

    await calendar.update(theirs, entry("Hair", "2026-10-07 00:00", "2026-10-08 00:00", all_day=True),
                          {"start", "end", "all_day"})                     # made to last all day, and back
    assert phone.lines(theirs, "DTSTART", "DTEND") == ["DTSTART;VALUE=DATE:20261007", "DTEND;VALUE=DATE:20261008"]
    await calendar.update(theirs, entry("Hair", "2026-10-07 16:30", "2026-10-07 17:15"), {"start", "end", "all_day"})
    assert phone.lines(theirs, "DTSTART") == ["DTSTART;TZID=Europe/London:20261007T163000"]
    (last,), _ = await calendar.changes(token, NOW)
    assert last.entry == after.entry


async def test_a_change_made_on_the_phone_in_between_is_not_overwritten_blindly(calendar, phone, monkeypatch):
    theirs = phone.put("hair", vevent("hair", "DTSTART:20261007T150000Z", "SUMMARY:Hairdresser"))
    read_file, reads = calendar._one, []

    async def read_then_the_phone_changes_it(path):
        found = await read_file(path)
        if not reads:      # the person moves it on the phone between our read and our write
            phone.put("hair", vevent("hair", "DTSTART:20261008T150000Z", "SUMMARY:Hairdresser"))
        reads.append(path)
        return found

    monkeypatch.setattr(calendar, "_one", read_then_the_phone_changes_it)
    await calendar.update(theirs, entry("Hair"), {"summary"})
    assert len(reads) == 2                                                 # refused once, read again, then written
    assert phone.lines(theirs, "DTSTART", "SUMMARY") == ["DTSTART:20261008T150000Z", "SUMMARY:Hair"]


async def test_deleting_what_is_already_gone_is_done_and_changing_what_is_gone_says_so(calendar, phone):
    theirs = phone.put("hair", vevent("hair", "DTSTART:20261007T150000Z", "SUMMARY:Hairdresser"))
    _, token = await calendar.changes(None, NOW)
    for _ in range(2):
        await calendar.delete(theirs)
    assert phone.files() == []
    assert await calendar.changes(token, NOW) == ([Remote(theirs, deleted=True)], (await calendar.changes(token, NOW))[1])
    with pytest.raises(Gone):
        await calendar.update(theirs, entry(), {"summary"})

    invited = phone.put("party", vevent("party", "DTSTART:20261009T180000Z", "SUMMARY:Party",
                                        "ORGANIZER:mailto:ola@example.test", "ATTENDEE:mailto:grace@example.test"))
    with pytest.raises(Refused):
        await calendar.delete(invited)                                     # deleting it would tell the guests
    with pytest.raises(Refused):
        await calendar.update(invited, entry("Party at ours"), {"summary"})
    assert phone.files() == [invited] and "SUMMARY:Party" in phone.read(invited)


async def test_a_date_moved_on_the_phone_is_its_own_event_and_goes_when_the_phone_removes_it(calendar, phone):
    series = vevent("football", "DTSTART;TZID=Europe/London:20261006T170000", "DTEND;TZID=Europe/London:20261006T180000",
                    "RRULE:FREQ=WEEKLY", "SUMMARY:Football")
    moved = vevent("football", "RECURRENCE-ID;TZID=Europe/London:20261013T170000",
                   "DTSTART;TZID=Europe/London:20261014T180000", "DTEND;TZID=Europe/London:20261014T190000",
                   "SUMMARY:Football")
    theirs = phone.put("football", series, moved)
    (weekly, one), token = await calendar.changes(None, NOW)
    assert (weekly.id, one.id) == (theirs, f"{theirs}#20261013")
    assert weekly.entry.exdates == (date(2026, 10, 13),) and wall(one.entry.start) == "Wed 14 Oct 18:00"
    followed = [weekly.id, one.id]

    # Changed here: its own VEVENT changes, and the series is as it was.
    await calendar.update(one.id, entry("Football", "2026-10-15 18:00", "2026-10-15 19:00"), {"start", "end"})
    assert phone.lines(theirs, "DTSTART") == ["DTSTART;TZID=Europe/London:20261006T170000",
                                              "DTSTART;TZID=Europe/London:20261015T180000"]
    (weekly, one), token = await calendar.changes(token, NOW, followed)
    assert wall(one.entry.start) == "Thu 15 Oct 18:00" and weekly.entry.exdates == (date(2026, 10, 13),)

    # The phone puts that date back the way the others are: it is no longer skipped, and no longer an event.
    phone.put("football", series)
    (weekly, gone), token = await calendar.changes(token, NOW, followed)
    assert weekly.entry.exdates == () and gone == Remote(f"{theirs}#20261013", deleted=True)
    assert [remote.id for remote in (await calendar.changes(token, NOW, [theirs]))[0]] == []

    # Moved again, then cancelled here: the series skips the date, as a calendar app writes it.
    phone.put("football", series, moved)
    _, token = await calendar.changes(token, NOW, followed)
    await calendar.delete(f"{theirs}#20261013")
    await calendar.delete(f"{theirs}#20261013")                            # already done
    assert phone.lines(theirs, "EXDATE", "RECURRENCE-ID") == ["EXDATE;TZID=Europe/London:20261013T170000"]
    (weekly, gone), token = await calendar.changes(token, NOW, followed)
    assert weekly.entry.exdates == (date(2026, 10, 13),) and gone.deleted

    # The whole series deleted on the phone takes its changed dates with it.
    phone.delete(theirs)
    found, _ = await calendar.changes(token, NOW, followed)
    assert found == [Remote(theirs, deleted=True), Remote(f"{theirs}#20261013", deleted=True)]


async def test_a_date_skipped_here_is_written_on_the_series_and_put_back_when_it_is_not(calendar, phone):
    weekly = entry("Football", "2026-10-06 17:00", "2026-10-06 18:00", rrule="FREQ=WEEKLY;BYDAY=TU")
    remote_id, _ = await calendar.create(weekly, EVENT_ID)
    skipping = replace(weekly, exdates=(date(2026, 10, 13),))
    await calendar.update(remote_id, skipping, {"exdates"})
    assert phone.lines(remote_id, "EXDATE") == ["EXDATE;TZID=Europe/London:20261013T170000"]
    await calendar.update(remote_id, weekly, {"exdates"})                  # undone here: the date is back there
    assert phone.lines(remote_id, "EXDATE") == []
    later = replace(skipping, start=london("2026-10-06 18:00"), end=london("2026-10-06 19:00"))
    await calendar.update(remote_id, later, {"start", "end", "exdates"})   # the skipped date moves with the series
    assert phone.lines(remote_id, "DTSTART", "EXDATE") == ["DTSTART;TZID=Europe/London:20261006T180000",
                                                           "EXDATE;TZID=Europe/London:20261013T180000"]
    once = replace(later, rrule=None, exdates=())
    await calendar.update(remote_id, once, {"rrule", "exdates"})           # no longer repeating
    assert phone.lines(remote_id, "RRULE", "EXDATE") == []


async def test_what_changed_since_is_asked_with_the_place_and_a_forgotten_place_says_start_again(calendar, phone):
    old = phone.put("old", vevent("old", "DTSTART:20260901T150000Z", "SUMMARY:Over already"))
    hair = phone.put("hair", vevent("hair", "DTSTART:20261007T150000Z", "SUMMARY:Hairdresser"))
    first, token = await calendar.changes(None, NOW)
    assert [remote.id for remote in first] == [hair]                       # what is over is not brought in

    phone.put("gp", vevent("gp", "DTSTART:20261008T093000Z", "SUMMARY:GP"))
    phone.put("hair", vevent("hair", "DTSTART:20261007T160000Z", "SUMMARY:Hairdresser"))
    phone.delete(old)
    changed, later = await calendar.changes(token, NOW)
    assert sorted((remote.id.rpartition("/")[2], remote.deleted) for remote in changed) == [
        ("gp.ics", False), ("hair.ics", False), ("old.ics", True)]
    assert later != token and await calendar.changes(later, NOW) == ([], later)

    with pytest.raises(SyncReset):
        await calendar.changes("a-place-the-server-never-gave", NOW)
    # Reading everything again: what is followed here and no longer there has gone. Without the
    # list of what is followed, a deletion made while the place was lost would never be seen.
    phone.delete(hair)
    again, _ = await calendar.changes(None, NOW, followed=[hair, f"{hair}#20261014"])
    assert {"hair.ics", "hair.ics#20261014"} <= {remote.id.rpartition("/")[2] for remote in again if remote.deleted}
    assert [remote.entry.summary for remote in again if not remote.deleted] == ["GP"]


async def test_a_calendar_with_more_events_than_one_request_holds_is_read_in_parts(calendar, phone, monkeypatch):
    monkeypatch.setattr(apple, "BATCH", 3)
    for number in range(8):
        phone.put(f"e{number}", vevent(f"e{number}", f"DTSTART:202610{10 + number}T150000Z", f"SUMMARY:Plan {number}"))
    found, _ = await calendar.changes(None, NOW)
    assert sorted(remote.entry.summary for remote in found) == [f"Plan {number}" for number in range(8)]
    monkeypatch.setattr(apple, "MAX_FILES", 5)
    with pytest.raises(Unavailable):
        await calendar.changes(None, NOW)


async def test_a_password_that_stops_working_is_a_lost_sign_in(icloud, calendar, phone):
    await calendar.changes(None, NOW)
    icloud.set_passwords({OLA: REPLACEMENT})                 # the one stored here was removed at Apple
    with pytest.raises(AuthLost):
        await calendar.changes(None, NOW)
    with pytest.raises(AuthLost):
        await calendar.create(entry(), EVENT_ID)


def test_what_is_written_reads_back_the_same_in_other_households_time_zones():
    for timezone in ("America/New_York", "Asia/Kolkata", "Australia/Sydney", "UTC"):
        weekly = entry("Football", "2026-10-06 17:00", "2026-10-06 18:00", rrule="FREQ=WEEKLY;UNTIL=20261215T235959Z",
                       exdates=(apple._day(london("2026-10-27 17:00"), timezone),), notes="Bring boots")
        start, end = apple.day_bounds(date(2026, 10, 9), timezone)
        day = Entry(summary="Inset day", start=start, end=end, all_day=True, rrule="FREQ=YEARLY",
                    exdates=(date(2027, 10, 9),))
        for sent in (weekly, day, entry()):
            (read,) = apple.remotes_of("/c/x.ics", None, apple.new_file("houseagent-x", sent, timezone, NOW), timezone)
            assert read.entry == sent, timezone
