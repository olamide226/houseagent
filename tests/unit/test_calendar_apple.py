"""An adult's own Apple Calendar, the whole way (ADR 0041): the stored sign-in, the worker job and
the real client, with a CalDAV server on this machine standing in for iCloud (tests/caldav.py).

Nothing is faked between the worker and that server, so what these tests show is that the sync
and the CalDAV client agree with each other and with a real CalDAV server. They do not show that
iCloud behaves as that server does: no Apple account existed to try it with.

What the sync itself does with any calendar is in test_calendar_sync.py, with a fake one.
"""
from datetime import date, timedelta

import pytest

from app.agent.actions import Recorder
from app.agent.prompt import build_brief
from app.agent.tools import run_tool
from app.calendars import apple
from app.config import get_settings
from app.db import execute, fetch_all, fetch_one, fetch_val, tx
from app.services import calendar, calendar_sync, households
from app.worker import jobs
from tests.caldav import PASSWORDS, REPLACEMENT, vevent
from tests.helpers import add_member, ctx_for, london, seed_home, wall
from tests.unit.test_dashboard import login
from tests.unit.test_loop import envelope

NOW = london("2026-10-05 12:00")     # a Monday
TICK = calendar_sync.PULL_EVERY
HAIR = ("DTSTART;TZID=Europe/London:20261007T160000", "DTEND;TZID=Europe/London:20261007T164500",
        "SUMMARY:Hairdresser", "LOCATION:Cut & Dry\\n4 High Street")


async def family():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_member(conn, home, "Ada", telegram_id="1002")
        await add_member(conn, home, "Tobi", role="child")
    return home


async def connect(home, icloud, who="Ola", password=None):
    """Sign `who` in to their Apple account and pick the Family calendar, as the Calendar page does.
    Returns their phone's calendar app on that calendar."""
    user = f"{who.lower()}@example.test"
    phone = icloud.phone(user)
    password = password or PASSWORDS[user]
    found = await apple.calendars(icloud.url, user, password)
    async with tx() as conn:
        await calendar_sync.begin(conn, home.id, home.members[who], "apple", user,
                                  calendar_sync.seal(get_settings(), username=user, password=password), found)
        await calendar_sync.choose(Recorder(ctx_for(conn, home, who, now=NOW)), phone.folder)
    return phone


async def rounds(n=1, *, start=0):
    """Run the worker job `n` times, two minutes apart, so each one reads the calendar as well."""
    for i in range(start, start + n):
        await jobs.sync_calendars(NOW + TICK * i + timedelta(seconds=i))


async def say(home, tool, who="Ola", **args):
    async with tx() as conn:
        result, is_error = await run_tool(tool, args, ctx_for(conn, home, who, now=NOW))
    assert not is_error, result
    return result


async def change(home, event_id, **fields):
    """Change one event by its id, as a turn does once it knows which one is meant."""
    async with tx() as conn:
        await calendar.modify_event(Recorder(ctx_for(conn, home, now=NOW)), event_id, scope="all", **fields)


async def events(status="active"):
    async with tx() as conn:
        return await fetch_all(conn, "select * from events where status = :status order by starts_at, title",
                               status=status)


async def plans():
    return [(event["title"], wall(event["starts_at"])) for event in await events()]


async def reminders():
    async with tx() as conn:
        rows = await fetch_all(conn, "select fire_at from reminders where status = 'scheduled' order by fire_at")
    return [wall(row["fire_at"]) for row in rows]


async def connection(who="Ola"):
    async with tx() as conn:
        return await fetch_one(conn, "select c.* from calendar_connections c join members m on m.id = c.member_id "
                                     "where m.name = :who", who=who)


async def logged():
    async with tx() as conn:
        return await fetch_val(conn, "select count(*) from agent_actions")


@pytest.fixture
def sent(monkeypatch):
    """Every request the client makes that changes something on the calendar."""
    writes: list[tuple[str, str]] = []
    once = apple._Dav._once

    async def noted(self, method, url, body, headers):
        if method in ("PUT", "DELETE"):
            writes.append((method, url.rpartition("/")[2]))
        return await once(self, method, url, body, headers)

    monkeypatch.setattr(apple._Dav, "_once", noted)
    return writes


# ---------------------------------------------------------------- from here to the calendar
async def test_a_plan_made_in_chat_is_in_apple_calendar_once_and_then_nothing_moves(icloud, sent):
    home = await family()
    phone = await connect(home, icloud)
    await say(home, "schedule_event", title="GP", starts_at="2026-10-07T10:30", participants=["Ada"],
              location="Health centre")
    await rounds(2)

    (file,) = phone.files()
    assert phone.lines(file, "SUMMARY", "DTSTART", "DTEND", "LOCATION") == [
        "SUMMARY:GP for Ada", "DTSTART;TZID=Europe/London:20261007T103000", "DTEND;TZID=Europe/London:20261007T113000",
        "LOCATION:Health centre"]
    assert "VALARM" not in phone.read(file)                     # the assistant sends the reminders, not the phone
    assert await reminders() == ["Tue 6 Oct 10:30", "Wed 7 Oct 09:30"]
    assert sent == [("PUT", file.rpartition("/")[2])] and await plans() == [("GP", "Wed 7 Oct 10:30")]

    actions = await logged()
    await rounds(4, start=2)                                    # read back four more times: no echo, no second copy
    assert len(sent) == 1 and len(phone.files()) == 1 and await logged() == actions
    assert await plans() == [("GP", "Wed 7 Oct 10:30")]
    stored = await connection()
    assert (stored["status"], stored["failures"], stored["last_error"]) == ("active", 0, None)
    assert stored["sync_token"] and stored["last_synced_at"]


async def test_moving_it_here_changes_the_file_cancelling_removes_it_and_undo_puts_it_back(icloud, sent):
    home = await family()
    phone = await connect(home, icloud)
    await say(home, "schedule_event", title="Dentist", starts_at="2026-10-09T14:00")
    await rounds()
    (file,) = phone.files()

    await say(home, "modify_event", event="dentist", starts_at="2026-10-09T15:30")
    await rounds(2, start=1)
    assert phone.lines(file, "DTSTART", "DTEND") == ["DTSTART;TZID=Europe/London:20261009T153000",
                                                     "DTEND;TZID=Europe/London:20261009T163000"]
    await say(home, "modify_event", event="dentist", cancel=True)
    await rounds(2, start=3)
    assert phone.files() == [] and await plans() == []
    await say(home, "undo_last")
    await rounds(2, start=5)
    assert phone.files() == [file] and await plans() == [("Dentist", "Fri 9 Oct 15:30")]
    assert [method for method, _ in sent] == ["PUT", "PUT", "DELETE", "PUT"]     # one request for each change


# ---------------------------------------------------------------- from the calendar to here
async def test_what_is_added_moved_and_deleted_on_the_phone_happens_here_and_is_not_sent_back(icloud, sent):
    home = await family()
    phone = await connect(home, icloud)
    hair = phone.put("hair", vevent("hair", *HAIR, "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Reminder",
                                    "TRIGGER:-PT30M", "END:VALARM"))
    await rounds()

    (event,) = await events()
    assert (event["title"], wall(event["starts_at"]), wall(event["ends_at"]), event["location"]) == (
        "Hairdresser", "Wed 7 Oct 16:00", "Wed 7 Oct 16:45", "Cut & Dry, 4 High Street")
    assert await reminders() == ["Tue 6 Oct 16:00", "Wed 7 Oct 15:00"]         # the assistant reminds its owner
    async with tx() as conn:
        (action,) = await fetch_all(conn, "select tool, source, result, member_id from agent_actions "
                                          "where source = 'calendar'")
    assert (action["tool"], action["member_id"]) == ("calendar.sync", home.ola)
    assert "NOTE: added in Ola's Apple Calendar" in action["result"]

    phone.put("hair", vevent("hair", "DTSTART;TZID=Europe/London:20261008T170000",
                             "DTEND;TZID=Europe/London:20261008T174500", "SUMMARY:Hairdresser"))
    await rounds(2, start=1)
    assert await plans() == [("Hairdresser", "Thu 8 Oct 17:00")]
    assert await reminders() == ["Wed 7 Oct 17:00", "Thu 8 Oct 16:00"] and (await events())[0]["location"] is None

    phone.delete(hair)
    await rounds(3, start=3)
    assert await plans() == [] and await reminders() == [] and phone.files() == []
    assert sent == []                                           # nothing here was ever written back to the phone


async def test_an_event_that_lasts_all_day_comes_in_without_reminders_and_goes_to_the_other_calendar_as_a_date(icloud):
    home = await family()
    olas, adas = await connect(home, icloud), await connect(home, icloud, "Ada")
    olas.put("inset", vevent("inset", "DTSTART;VALUE=DATE:20261009", "DTEND;VALUE=DATE:20261010", "SUMMARY:Inset day"))
    await rounds(2)
    (event,) = await events()
    assert event["all_day"] and (wall(event["starts_at"]), wall(event["ends_at"])) == ("Fri 9 Oct 00:00", "Sat 10 Oct 00:00")
    assert await reminders() == []
    (copy,) = adas.files()
    assert adas.lines(copy, "DTSTART", "DTEND", "SUMMARY") == [
        "DTSTART;VALUE=DATE:20261009", "DTEND;VALUE=DATE:20261010", "SUMMARY:Inset day"]


async def test_two_adults_apple_calendars_each_get_the_others_plans_once_and_then_nothing_moves(icloud, sent):
    home = await family()
    olas, adas = await connect(home, icloud), await connect(home, icloud, "Ada")
    olas.put("hair", vevent("hair", *HAIR))
    adas.put("swim", vevent("swim", "DTSTART;TZID=Europe/London:20261008T160000", "SUMMARY:Tobi swimming"))
    await say(home, "schedule_event", title="Boiler service", starts_at="2026-10-08T14:00")
    await rounds(3)

    assert [title for title, _ in await plans()] == ["Hairdresser", "Boiler service", "Tobi swimming"]
    for phone in (olas, adas):
        titles = sorted(phone.lines(file, "SUMMARY")[0] for file in phone.files())
        assert titles == ["SUMMARY:Boiler service", "SUMMARY:Hairdresser", "SUMMARY:Tobi swimming"]
    assert sorted(method for method, _ in sent) == ["PUT"] * 4       # the chat plan to both, and one each way
    actions = await logged()
    await rounds(4, start=3)
    assert len(sent) == 4 and await logged() == actions and len(await events()) == 3
    async with tx() as conn:
        assert await fetch_val(conn, "select count(*) from calendar_links") == 6


# ---------------------------------------------------------------- repeats, and one date changed by itself
async def test_a_weekly_activity_goes_over_with_its_rule_and_a_week_skipped_or_put_back_here_is_too(icloud):
    home = await family()
    phone = await connect(home, icloud)
    await say(home, "schedule_event", title="Chatterbox", kind="activity", starts_at="2026-10-06T09:00",
              rrule="FREQ=WEEKLY;BYDAY=TU")
    await rounds()
    (file,) = phone.files()
    assert phone.lines(file, "DTSTART", "RRULE", "EXDATE") == ["DTSTART;TZID=Europe/London:20261006T090000",
                                                              "RRULE:FREQ=WEEKLY;BYDAY=TU"]
    await say(home, "modify_event", event="chatterbox", cancel=True, scope="this", occurrence="2026-10-27")
    await rounds(2, start=1)                                    # the week after the clocks change
    assert phone.lines(file, "EXDATE") == ["EXDATE;TZID=Europe/London:20261027T090000"]
    await say(home, "undo_last")
    await rounds(2, start=3)
    assert phone.lines(file, "EXDATE") == [] and (await events())[0]["exdates"] == []


async def test_one_date_moved_on_the_phone_is_skipped_in_the_series_and_is_a_plan_of_its_own(icloud, sent):
    home = await family()
    phone = await connect(home, icloud)
    series = vevent("football", "DTSTART;TZID=Europe/London:20261006T170000", "DTEND;TZID=Europe/London:20261006T180000",
                    "RRULE:FREQ=WEEKLY;BYDAY=TU", "SUMMARY:Football")
    moved = vevent("football", "RECURRENCE-ID;TZID=Europe/London:20261013T170000",
                   "DTSTART;TZID=Europe/London:20261014T180000", "DTEND;TZID=Europe/London:20261014T190000",
                   "SUMMARY:Football")
    file = phone.put("football", series)
    await rounds()
    (weekly,) = await events()
    assert weekly["rrule"] == "FREQ=WEEKLY;BYDAY=TU" and weekly["exdates"] == []

    phone.put("football", series, moved)                        # the phone moves the second week to Wednesday
    await rounds(3, start=1)
    weekly, one = await events()
    assert weekly["exdates"] == [date(2026, 10, 13)]
    assert (one["title"], wall(one["starts_at"]), one["rrule"]) == ("Football", "Wed 14 Oct 18:00", None)
    assert "Tue 13 Oct 16:00" not in await reminders() and "Wed 14 Oct 17:00" in await reminders()
    assert sent == [] and phone.files() == [file]               # nothing sent back, and no second file

    await change(home, one["id"], starts_at=london("2026-10-14 19:00"))
    await rounds(2, start=4)                                    # moved again, in chat: its own event in the file changes
    assert phone.lines(file, "DTSTART", "RECURRENCE-ID") == [
        "DTSTART;TZID=Europe/London:20261006T170000", "DTSTART;TZID=Europe/London:20261014T190000",
        "RECURRENCE-ID;TZID=Europe/London:20261013T170000"]
    assert sent == [("PUT", "football.ics")] and len(await events()) == 2

    # The phone deletes the moved date: the plan of its own goes, and the series still skips that Tuesday.
    phone.put("football", series.replace("SUMMARY:Football", "EXDATE;TZID=Europe/London:20261013T170000\r\nSUMMARY:Football"))
    await rounds(2, start=6)
    (weekly,) = await events()
    assert weekly["exdates"] == [date(2026, 10, 13)] and len(await events("cancelled")) == 1

    phone.put("football", series, moved)                        # moved once more, and then the whole series deleted
    await rounds(2, start=8)
    assert len(await events()) == 2
    phone.delete(file)
    await rounds(2, start=10)
    assert await events() == [] and await reminders() == [] and len(sent) == 1


async def test_a_moved_date_cancelled_in_chat_is_skipped_on_the_phone_and_a_date_put_back_there_is_back_here(icloud):
    home = await family()
    phone = await connect(home, icloud)
    series = vevent("football", "DTSTART;TZID=Europe/London:20261006T170000", "RRULE:FREQ=WEEKLY;BYDAY=TU",
                    "SUMMARY:Football")
    file = phone.put("football", series, vevent(
        "football", "RECURRENCE-ID;TZID=Europe/London:20261013T170000", "DTSTART;TZID=Europe/London:20261014T180000",
        "SUMMARY:Football"))
    await rounds()
    one = next(event for event in await events() if not event["rrule"])
    await change(home, one["id"], cancel=True)
    await rounds(2, start=1)
    assert phone.lines(file, "RECURRENCE-ID", "EXDATE") == ["EXDATE;TZID=Europe/London:20261013T170000"]
    (weekly,) = await events()
    assert weekly["exdates"] == [date(2026, 10, 13)]

    phone.put("football", series)                               # on the phone the date is put back as it was
    await rounds(2, start=3)
    (weekly,) = await events()
    assert weekly["exdates"] == [] and phone.lines(file, "EXDATE") == []


# ---------------------------------------------------------------- when it goes wrong
async def test_a_password_removed_at_apple_stops_it_tells_its_owner_once_and_a_new_one_carries_on(icloud, sent):
    home = await family()
    phone = await connect(home, icloud, "Ada")
    await say(home, "schedule_event", title="Dentist", starts_at="2026-10-09T14:00")
    await rounds()
    icloud.set_passwords({**PASSWORDS, "ada@example.test": REPLACEMENT})
    phone.sign_in(REPLACEMENT)

    assert await jobs.sync_calendars(NOW + TICK) == 1
    assert await jobs.sync_calendars(NOW + TICK * 2) == 0           # stopped: it is not tried again
    stored = await connection("Ada")
    assert (stored["status"], stored["last_error"]) == ("broken", "AuthLost: 401")
    async with tx() as conn:
        (told,) = await fetch_all(conn, "select target, member_id, text from outbox")
        brief = await build_brief(conn, envelope(home, "hi").model_copy(
            update={"member_id": home.members["Ada"], "member_name": "Ada"}), NOW)
    assert (told["target"], told["member_id"]) == ("member", home.members["Ada"])
    assert told["text"] == (
        "I can no longer reach your Apple Calendar, so plans are not being copied between it and me. To fix it, "
        "send me the word dashboard, open Calendar and tap Connect again. It needs a new app-specific password "
        "from Apple: the page shows how to make one.")
    assert "own Apple Calendar has stopped working: they tap Connect again on the Calendar page" in brief

    # With the new password and the same calendar it carries on with what was linked: no second copy.
    await say(home, "schedule_event", title="Optician", starts_at="2026-10-12T11:00")
    phone.put("hair", vevent("hair", *HAIR))
    await connect(home, icloud, "Ada", password=REPLACEMENT)
    await rounds(2, start=3)
    assert (await connection("Ada"))["status"] == "active" and len(phone.files()) == 3
    assert [title for title, _ in await plans()] == ["Hairdresser", "Dentist", "Optician"]
    assert [method for method, _ in sent] == ["PUT", "PUT"]


async def test_a_deletion_on_the_phone_while_the_place_in_its_changes_was_lost_is_still_seen(icloud, sent):
    home = await family()
    phone = await connect(home, icloud)
    kept = phone.put("hair", vevent("hair", *HAIR))
    gone = phone.put("optician", vevent("optician", "DTSTART;TZID=Europe/London:20261012T110000", "SUMMARY:Optician"))
    await say(home, "schedule_event", title="Dentist", starts_at="2026-10-09T14:00")
    await rounds(2)
    assert len(await events()) == 3 and len(phone.files()) == 3

    phone.delete(gone)
    async with tx() as conn:                                    # the server no longer knows the place we hold
        await execute(conn, "update calendar_connections set sync_token = 'a-place-the-server-never-gave'")
    await rounds(2, start=2)
    assert [title for title, _ in await plans()] == ["Hairdresser", "Dentist"]      # CalDAV says "gone" by leaving it out
    assert len(phone.files()) == 2 and kept in phone.files() and [method for method, _ in sent] == ["PUT"]
    assert (await connection())["sync_token"] != "a-place-the-server-never-gave"


async def test_an_event_with_guests_is_a_plan_here_but_a_change_made_here_is_not_sent_to_it(icloud, sent):
    home = await family()
    phone = await connect(home, icloud)
    party = phone.put("party", vevent("party", "DTSTART;TZID=Europe/London:20261009T190000", "SUMMARY:Party",
                                      "ORGANIZER;CN=Ola:mailto:ola@example.test",
                                      "ATTENDEE;CN=Grace;PARTSTAT=NEEDS-ACTION:mailto:grace@example.test"))
    await rounds()
    assert await plans() == [("Party", "Fri 9 Oct 19:00")]
    await say(home, "modify_event", event="party", location="Ours")
    await rounds(3, start=1)
    assert "LOCATION" not in phone.read(party) and sent == []       # iCloud would have emailed Grace
    assert ((await connection())["failures"], (await events())[0]["location"]) == (0, "Ours")   # and nothing waits


# ---------------------------------------------------------------- the whole way, from the page
async def test_from_the_form_on_the_calendar_page_to_a_plan_on_the_phone_with_the_password_kept_secret(
        client, icloud, capfd):
    home = await family()
    phone = icloud.phone("ola@example.test")
    password = PASSWORDS["ola@example.test"]
    csrf = await login(client, home)
    typed = await client.post("/dashboard/calendar/connect/apple", headers=csrf,
                              data={"apple_id": " ola@example.test ", "password": f" {password[:4]} {password[4:]} "})
    assert "Which calendar do you want to share?" in typed.text and password not in typed.text
    chosen = await client.post("/dashboard/calendar/own/choose", data={"calendar_id": phone.folder}, headers=csrf)
    assert "Bringing your plans over" in chosen.text
    async with tx() as conn:        # the page's clock is the real one; the rounds below run on the tests' clock
        await execute(conn, "update calendar_connections set next_pull_at = :now", now=NOW)

    phone.put("hair", vevent("hair", *HAIR))
    await say(home, "schedule_event", title="Dentist", starts_at="2026-10-09T14:00")
    await rounds(2)
    assert await plans() == [("Hairdresser", "Wed 7 Oct 16:00"), ("Dentist", "Fri 9 Oct 14:00")]
    assert len(phone.files()) == 2
    page = (await client.get("/dashboard/calendar")).text
    assert '<span class="pill good">Connected</span>' in page and "Apple Calendar · ola@example.test" in page

    # The password is encrypted where it is kept, and is in no export, no logged action and no log line.
    stored = await connection()
    assert password not in stored["secret"] and stored["account"] == "ola@example.test"
    assert calendar_sync.unseal(get_settings(), stored["secret"]) == {"username": "ola@example.test",
                                                                      "password": password}
    icloud.set_passwords({})                                    # and when it stops working, the reason names no secret
    await jobs.sync_calendars(NOW + TICK * 3)
    async with tx() as conn:
        exported = await households.export(conn, home.id)
        everything = await fetch_all(conn, "select tool, args, result from agent_actions")
        broken = await fetch_one(conn, "select status, last_error from calendar_connections")
    assert "secret" not in exported["calendar_connections"][0] and password not in str(exported)
    assert stored["secret"] not in str(exported) and password not in str(everything)
    assert {"calendar.connect", "calendar.sync"} <= {action["tool"] for action in everything}
    assert broken == {"status": "broken", "last_error": "AuthLost: 401"}
    out, err = capfd.readouterr()
    assert password not in out + err and "calendar_connection_broken" in out + err


async def test_the_series_made_here_keeps_one_file_when_the_phone_changes_a_date_of_it(icloud, sent):
    """Our own series with one date changed on the phone: the changed date is not taken for a second
    copy of the series and deleted, which a mark on every VEVENT would have caused."""
    home = await family()
    phone = await connect(home, icloud)
    await say(home, "schedule_event", title="Chatterbox", kind="activity", starts_at="2026-10-06T09:00",
              rrule="FREQ=WEEKLY;BYDAY=TU")
    await rounds()
    (file,) = phone.files()
    uid = phone.lines(file, "UID")[0].removeprefix("UID:")
    phone.put(file.rpartition("/")[2].removesuffix(".ics"),
              vevent(uid, "DTSTART;TZID=Europe/London:20261006T090000", "DTEND;TZID=Europe/London:20261006T100000",
                     "RRULE:FREQ=WEEKLY;BYDAY=TU", "SUMMARY:Chatterbox"),
              vevent(uid, "RECURRENCE-ID;TZID=Europe/London:20261013T090000",
                     "DTSTART;TZID=Europe/London:20261013T110000", "DTEND;TZID=Europe/London:20261013T120000",
                     "SUMMARY:Chatterbox"))
    await rounds(3, start=1)
    weekly, one = sorted(await events(), key=lambda event: event["rrule"] is None)
    assert weekly["exdates"] == [date(2026, 10, 13)] and wall(one["starts_at"]) == "Tue 13 Oct 11:00"
    assert phone.files() == [file] and len(phone.lines(file, "RECURRENCE-ID")) == 1
    assert [method for method, _ in sent] == ["PUT"]            # only the first copy: nothing deleted, nothing echoed
    async with tx() as conn:
        assert await calendar.get_event(conn, home.id, weekly["id"]) is not None
        assert await fetch_val(conn, "select count(*) from calendar_links") == 2
