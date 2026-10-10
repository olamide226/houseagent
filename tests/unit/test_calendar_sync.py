"""Two-way calendar sync (ADR 0036): what a connected calendar and the household's events do to
each other, judged by rows here and by what the calendar was asked to do.

The calendar is `FakeCalendar`, an in-memory one that keeps etags and a list of changes the way a
provider does. Google's own wire format is checked in tests/contract/test_google_calendar.py.
"""
from dataclasses import replace
from datetime import date, timedelta

import pytest

from app.agent import actions
from app.agent.actions import Recorder
from app.agent.prompt import build_brief
from app.agent.tools import run_tool
from app.calendars.base import AuthLost, Entry, Gone, Refused, Remote, RemoteCalendar, SyncReset, Unavailable
from app.config import get_settings
from app.core import crypto
from app.db import execute, fetch_all, fetch_one, fetch_val, tx
from app.services import calendar, calendar_sync, households
from app.worker import jobs
from tests.helpers import add_member, ctx_for, london, seed_home, wall
from tests.unit.test_loop import envelope

NOW = london("2026-10-05 12:00")     # a Monday
TICK = calendar_sync.PULL_EVERY


class FakeCalendar:
    """A calendar as a provider keeps one: events with etags, and every change in order, so
    "what changed since" can be answered. `calls` is what the sync asked it to do."""

    def __init__(self) -> None:
        self.events: dict[str, Remote] = {}
        self.history: list[str] = []          # the id touched by each change, oldest first
        self.calls: list[tuple] = []
        self.fail: Exception | None = None    # raised by the next read
        self.refuse: set[str] = set()         # summaries the calendar will not take
        self.forgotten = False                # the next read with a token is answered "start again"
        self.reads: list[str | None] = []
        self._n = 0

    async def __aenter__(self) -> "FakeCalendar":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def _put(self, remote: Remote) -> Remote:
        self._n += 1
        self.events[remote.id] = stored = replace(remote, etag=f"etag-{self._n}")
        self.history.append(remote.id)
        return stored

    # What a person does in their calendar app.
    def add(self, summary: str, start: str, *, minutes: int = 60, **more) -> str:
        self._n += 1
        fields = {name: more.pop(name) for name in ("series_id", "original", "owner") if name in more}
        begins = london(start)
        entry = Entry(summary=summary, start=begins, end=begins + timedelta(minutes=minutes), **more)
        return self._put(Remote(f"theirs-{self._n}", entry=entry, **fields)).id

    def edit(self, remote_id: str, **fields) -> None:
        was = self.events[remote_id]
        if "start" in fields:
            length = was.entry.end - was.entry.start
            fields["start"] = london(fields["start"])
            fields.setdefault("end", fields["start"] + length)
        self._put(replace(was, entry=replace(was.entry, **fields)))

    def remove(self, remote_id: str) -> None:
        self._put(replace(self.events[remote_id], deleted=True, entry=None))

    def summaries(self) -> list[str]:
        return sorted(remote.entry.summary for remote in self.events.values() if not remote.deleted)

    def named(self, summary: str) -> Remote:
        (found,) = [remote for remote in self.events.values() if not remote.deleted and remote.entry.summary == summary]
        return found

    def did(self, what: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == what]

    # What the sync asks of it (`Provider`).
    async def changes(self, sync_token, since):
        self.reads.append(sync_token)
        if self.fail:
            failure, self.fail = self.fail, None
            raise failure
        if sync_token and self.forgotten:
            self.forgotten = False
            raise SyncReset
        ids = list(dict.fromkeys(self.history[int(sync_token):] if sync_token else self.history))
        found = [self.events[remote_id] for remote_id in ids]
        if not sync_token:   # a first read: only what ends after `since`, and what was deleted
            found = [remote for remote in found
                     if remote.entry is None or remote.entry.rrule or remote.entry.end > since]
        return sorted(found, key=lambda remote: remote.series_id is not None), str(len(self.history))

    async def create(self, entry, owner):
        if entry.summary in self.refuse:
            raise Refused("forbidden")
        self._n += 1
        made = self._put(Remote(f"ours-{self._n}", entry=entry, owner=owner))
        self.calls.append(("create", made.id, entry))
        return made.id, made.etag

    async def update(self, remote_id, entry, changed):
        was = self.events.get(remote_id)
        if was is None or was.deleted:
            raise Gone
        self.calls.append(("update", remote_id, sorted(changed)))
        merged = replace(was.entry, **{name: getattr(entry, name) for name in changed})
        return self._put(replace(was, entry=merged)).etag

    async def delete(self, remote_id):
        self.calls.append(("delete", remote_id))
        if remote_id in self.events and not self.events[remote_id].deleted:
            self.remove(remote_id)


@pytest.fixture
def calendars(monkeypatch):
    """Every connection's calendar, by the address it was connected with."""
    fakes: dict[str, FakeCalendar] = {}

    def provider_for(settings, connection):
        calendar_sync.unseal(settings, connection["secret"])   # as the real one does: the sign-in must open
        return fakes.setdefault(connection["account"], FakeCalendar())

    monkeypatch.setattr(calendar_sync, "provider_for", provider_for)
    return fakes


async def family(conn):
    home = await seed_home(conn)
    await add_member(conn, home, "Ada", telegram_id="1002")
    await add_member(conn, home, "Tobi", role="child")
    return home


async def connect(home, who="Ola", calendar_id="family", name="Family", calendars=None):
    """Sign `who` in and pick a calendar, as the two steps on the Calendar page do."""
    account = f"{who.lower()}@example.test"
    async with tx() as conn:
        await calendar_sync.begin(
            conn, home.id, home.members[who], "google", account,
            calendar_sync.seal(get_settings(), refresh_token=f"refresh-{who}"), [RemoteCalendar(calendar_id, name)])
        await calendar_sync.choose(Recorder(ctx_for(conn, home, who, now=NOW)), calendar_id)
    if calendars is not None:
        return calendars.setdefault(account, FakeCalendar())


async def rounds(n=1, *, start=0):
    """Run the worker job `n` times, two minutes apart, so each one reads the calendar as well."""
    for i in range(start, start + n):
        await jobs.sync_calendars(NOW + TICK * i + timedelta(seconds=i))


async def say(home, tool, who="Ola", **args):
    async with tx() as conn:
        result, is_error = await run_tool(tool, args, ctx_for(conn, home, who, now=NOW))
    assert not is_error, result
    return result


async def events(status="active"):
    async with tx() as conn:
        return await fetch_all(
            conn, """select e.*, coalesce((select array_agg(m.name order by m.name) from members m
                                           where m.id = any(e.participant_ids)), '{}') as people
                     from events e where status = :status order by starts_at, title""", status=status)


async def reminders():
    async with tx() as conn:
        rows = await fetch_all(
            conn, """select r.fire_at, r.text, r.target, m.name from reminders r left join members m on m.id = r.member_id
                     where r.status = 'scheduled' order by r.fire_at""")
    return [(wall(r["fire_at"]), r["text"], r["target"], r["name"]) for r in rows]


async def connection(who="Ola"):
    async with tx() as conn:
        return await fetch_one(conn, "select c.* from calendar_connections c join members m on m.id = c.member_id "
                                     "where m.name = :who", who=who)


# ---------------------------------------------------------------- from here to the calendar
async def test_a_plan_made_in_chat_appears_on_the_calendar_once_and_is_never_sent_again(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    await say(home, "schedule_event", title="GP", starts_at="2026-10-07T10:30", participants=["Ada"],
              location="Hurley Clinic")
    await rounds(3)

    ((_, remote_id, sent),) = theirs.did("create")
    assert (sent.summary, wall(sent.start), wall(sent.end), sent.location, sent.rrule, sent.all_day) == (
        "GP for Ada", "Wed 7 Oct 10:30", "Wed 7 Oct 11:30", "Hurley Clinic", None, False)
    (event,) = await events()
    assert theirs.events[remote_id].owner == event["id"]          # marked as made from this event
    assert theirs.did("update") == [] and theirs.did("delete") == []   # its own echo changed nothing
    async with tx() as conn:
        (link,) = await fetch_all(conn, "select * from calendar_links")
        logged = await fetch_all(conn, "select tool, source from agent_actions order by created_at")
    assert link["event_id"] == event["id"] and link["remote_id"] == remote_id
    assert logged == [{"tool": "schedule_event", "source": "agent"}]      # sending is not a change to the household


async def test_moving_an_event_sends_only_what_changed_and_cancelling_removes_it_there(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    await say(home, "schedule_event", title="Dentist", starts_at="2026-10-09T14:00", location="High Street")
    await rounds()
    remote_id = theirs.named("Dentist").id

    await say(home, "modify_event", event="dentist", starts_at="2026-10-09T15:30")
    await rounds(start=1)
    assert theirs.did("update") == [("update", remote_id, ["end", "start"])]
    assert wall(theirs.named("Dentist").entry.start) == "Fri 9 Oct 15:30"
    assert theirs.named("Dentist").entry.location == "High Street"

    await say(home, "modify_event", event="dentist", cancel=True)
    await rounds(2, start=2)
    assert theirs.did("delete") == [("delete", remote_id)] and theirs.summaries() == []
    async with tx() as conn:
        assert await fetch_all(conn, "select 1 from calendar_links") == []
    assert [e["title"] for e in await events("cancelled")] == ["Dentist"]   # the calendar's own "deleted" changed nothing


async def test_undo_in_chat_takes_the_event_off_the_calendar_and_undoing_a_cancel_puts_it_back(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    await say(home, "schedule_event", title="Boiler service", starts_at="2026-10-08T14:00")
    await rounds()
    assert theirs.summaries() == ["Boiler service"]

    await say(home, "undo_last")                    # the event row is deleted outright
    await rounds(start=1)
    assert theirs.summaries() == [] and await events() == []

    await say(home, "schedule_event", title="Boiler service", starts_at="2026-10-08T14:00")
    await say(home, "modify_event", event="boiler", cancel=True)
    await rounds(start=2)
    assert theirs.summaries() == []
    await say(home, "undo_last")                    # the cancellation is taken back
    await rounds(start=3)
    assert theirs.summaries() == ["Boiler service"] and len(await events()) == 1


async def test_what_is_already_planned_goes_over_when_a_calendar_is_connected_but_not_what_is_over(calendars):
    async with tx() as conn:
        home = await family(conn)
        rec = Recorder(ctx_for(conn, home, now=NOW - timedelta(days=7)))
        await calendar.schedule_event(rec, title="Last week's dentist", starts_at=london("2026-09-30 09:00"))
        await calendar.schedule_event(rec, title="Chatterbox", starts_at=london("2026-09-29 09:00"),
                                      rrule="FREQ=WEEKLY;BYDAY=TU", participant_ids=[home.members["Tobi"]])
        await calendar.schedule_event(rec, title="Finished course", starts_at=london("2026-09-01 18:00"),
                                      rrule="FREQ=WEEKLY;UNTIL=20260922T235959")
        await calendar.schedule_event(rec, title="School play", starts_at=london("2026-10-20 18:00"))
    theirs = await connect(home, calendars=calendars)
    await rounds(2)
    assert theirs.summaries() == ["Chatterbox for Tobi", "School play"]
    assert theirs.named("Chatterbox for Tobi").entry.rrule == "FREQ=WEEKLY;BYDAY=TU"


# ---------------------------------------------------------------- from the calendar to here
async def test_an_event_added_on_the_calendar_becomes_a_plan_with_reminders_for_its_owner(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, "Ada", calendars=calendars)
    theirs.add("Hairdresser", "2026-10-07 16:00", minutes=45, location="Cut & Dry", notes="Ask for Sam")
    theirs.add("Last month's thing", "2026-09-07 16:00")
    await rounds(2)

    (event,) = await events()
    assert (event["title"], wall(event["starts_at"]), wall(event["ends_at"]), event["location"], event["notes"]) == (
        "Hairdresser", "Wed 7 Oct 16:00", "Wed 7 Oct 16:45", "Cut & Dry", "Ask for Sam")
    assert event["kind"] == "appointment" and event["created_by"] == home.members["Ada"] and not event["all_day"]
    assert await reminders() == [                      # nobody is named, so it is said to whoever's calendar it is
        ("Tue 6 Oct 16:00", "Hairdresser tomorrow at 16:00, Cut & Dry", "member", "Ada"),
        ("Wed 7 Oct 15:00", "Hairdresser today at 16:00, Cut & Dry", "member", "Ada")]
    assert theirs.calls == []                          # nothing is sent back for what came from there
    async with tx() as conn:
        (action,) = await fetch_all(conn, "select * from agent_actions where tool = 'calendar.sync'")
    assert action["source"] == "calendar" and action["member_id"] == home.members["Ada"]
    assert action["result"].splitlines() == [
        "OK: Hairdresser on Wed 7 Oct at 16:00, Cut & Dry",
        "NOTE: reminders at Tue 6 Oct 16:00, Wed 7 Oct 15:00", "NOTE: added in Ada's Google Calendar"]


async def test_a_title_that_names_a_child_is_theirs_and_is_said_to_the_household(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    theirs.add("Tobi swimming", "2026-10-08 17:00")
    await rounds()
    (event,) = await events()
    assert event["people"] == ["Tobi"]
    assert {(target, name) for _, _, target, name in await reminders()} == {("household", None)}
    assert theirs.calls == []                          # "Tobi swimming" already names Tobi: nothing to send back


async def test_a_change_on_the_calendar_moves_the_plan_and_its_reminders_and_is_not_echoed(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    remote_id = theirs.add("Hairdresser", "2026-10-07 16:00")
    await rounds()
    theirs.edit(remote_id, start="2026-10-08 09:30", summary="Haircut", location="Cut & Dry")
    await rounds(3, start=1)

    (event,) = await events()
    assert (event["title"], wall(event["starts_at"]), wall(event["ends_at"]), event["location"]) == (
        "Haircut", "Thu 8 Oct 09:30", "Thu 8 Oct 10:30", "Cut & Dry")
    assert [(at, text) for at, text, _, _ in await reminders()] == [
        ("Wed 7 Oct 09:30", "Haircut tomorrow at 09:30, Cut & Dry"),
        ("Thu 8 Oct 08:30", "Haircut today at 09:30, Cut & Dry")]
    assert theirs.calls == []
    async with tx() as conn:
        results = await fetch_all(conn, "select result from agent_actions where tool = 'calendar.sync' "
                                        "order by created_at")
    assert len(results) == 2 and results[1]["result"].splitlines()[-1] == "NOTE: changed in Ola's Google Calendar"


async def test_deleting_on_the_calendar_cancels_the_plan_and_its_reminders_and_it_is_not_made_again(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    await say(home, "schedule_event", title="Dentist", starts_at="2026-10-09T14:00")
    await rounds()
    theirs.remove(theirs.named("Dentist").id)
    await rounds(3, start=1)

    assert await events() == [] and [e["title"] for e in await events("cancelled")] == ["Dentist"]
    assert await reminders() == []
    assert len(theirs.did("create")) == 1 and theirs.summaries() == []


async def test_a_calendar_change_is_logged_as_its_owners_but_chat_undo_leaves_it_and_activity_can_undo_it(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    await say(home, "update_shopping_list", add=[{"item": "rice"}])
    theirs.add("Hairdresser", "2026-10-07 16:00")
    await rounds()

    async with tx() as conn:
        mine = await actions.undoable(conn, home.id, home.ola, 5)
        brief = await build_brief(conn, envelope(home, "undo"), NOW)
    assert [action["result"].splitlines()[0] for action in mine] == ["OK: rice added to shopping list"]
    assert "Last change by Ola (what undo_last reverts): OK: rice added" in brief
    await say(home, "undo_last")
    assert [e["title"] for e in await events()] == ["Hairdresser"]      # "undo" took back the rice, not the haircut

    async with tx() as conn:
        action_id = await fetch_val(conn, "select id from agent_actions where tool = 'calendar.sync'")
        await actions.undo_action(ctx_for(conn, home, "Ada", source="dashboard"), action_id)
    await rounds(start=1)
    assert await events() == [] and theirs.summaries() == []            # undone here, so removed there as well


# ---------------------------------------------------------------- both at once, and the same thing twice
async def test_changes_to_different_details_on_both_sides_are_both_kept_and_the_same_detail_is_the_calendars(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    remote_id = theirs.add("Parents evening", "2026-10-14 18:00", location="School hall")
    await rounds()

    theirs.edit(remote_id, start="2026-10-14 18:30")                      # the time, on the phone
    await say(home, "modify_event", event="parents evening", location="Room 4")   # the place, in chat
    await rounds(2, start=1)
    (event,) = await events()
    assert (wall(event["starts_at"]), event["location"]) == ("Wed 14 Oct 18:30", "Room 4")
    assert theirs.did("update") == [("update", remote_id, ["location"])]
    assert (wall(theirs.events[remote_id].entry.start), theirs.events[remote_id].entry.location) == (
        "Wed 14 Oct 18:30", "Room 4")

    theirs.edit(remote_id, location="Library")
    await say(home, "modify_event", event="parents evening", location="Gym")
    await rounds(2, start=3)
    (event,) = await events()
    assert event["location"] == "Library" and theirs.events[remote_id].entry.location == "Library"
    assert len(theirs.did("update")) == 1


async def test_a_plan_said_in_chat_that_is_also_on_the_calendar_is_one_plan_not_two(calendars):
    async with tx() as conn:
        home = await family(conn)
    await say(home, "schedule_event", title="Dentist", starts_at="2026-10-09T14:00")
    theirs = await connect(home, calendars=calendars)
    theirs.add("dentist", "2026-10-09 14:00")
    await rounds(3)
    assert [e["title"] for e in await events()] == ["Dentist"]
    assert theirs.did("create") == [] and len(theirs.events) == 1
    assert theirs.summaries() == ["Dentist"]             # the calendar's copy takes the household's spelling


async def test_an_event_whose_answer_was_lost_is_known_again_and_a_second_copy_is_removed(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    await say(home, "schedule_event", title="Dentist", starts_at="2026-10-09T14:00")
    (event,) = await events()
    # The calendar made the event, but the answer never arrived: nothing here says so.
    lost = theirs.add("Dentist", "2026-10-09 14:00", owner=event["id"])
    await rounds()
    assert theirs.did("create") == [] and theirs.summaries() == ["Dentist"]
    async with tx() as conn:
        assert await fetch_val(conn, "select remote_id from calendar_links") == lost

    theirs.add("Dentist", "2026-10-09 14:00", owner=event["id"])       # and a second copy of the same event
    await rounds(2, start=1)
    assert len(await events()) == 1 and theirs.summaries() == ["Dentist"]
    assert [remote_id for _, remote_id in theirs.did("delete")] != [lost]


async def test_another_installations_mark_is_no_reason_to_delete_or_adopt_an_event(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    theirs.add("Dentist", "2026-10-09 14:00", owner="6f1f8d0e-0000-4000-8000-000000000000")
    theirs.add("Optician", "2026-10-12 11:00", owner="not-an-id")
    await rounds(2)
    assert [e["title"] for e in await events()] == ["Dentist", "Optician"]     # brought in like any other
    assert theirs.calls == []


async def test_two_adults_calendars_each_get_the_others_events_once_and_then_nothing_moves(calendars):
    async with tx() as conn:
        home = await family(conn)
    olas = await connect(home, "Ola", "ola-main", "Ola", calendars=calendars)
    adas = await connect(home, "Ada", "family", "Family", calendars=calendars)
    olas.add("Dentist", "2026-10-09 14:00")
    adas.add("Tobi swimming", "2026-10-08 17:00")
    await say(home, "schedule_event", title="Boiler service", starts_at="2026-10-08T14:00")
    await rounds(4)

    assert [e["title"] for e in await events()] == ["Boiler service", "Tobi swimming", "Dentist"]
    assert olas.summaries() == adas.summaries() == ["Boiler service", "Dentist", "Tobi swimming"]
    assert sorted(call[2].summary for call in olas.did("create")) == ["Boiler service", "Tobi swimming"]
    assert sorted(call[2].summary for call in adas.did("create")) == ["Boiler service", "Dentist"]
    writes = len(olas.calls) + len(adas.calls)
    await rounds(4, start=4)
    assert len(olas.calls) + len(adas.calls) == writes == 4      # settled: no update, no delete, no ping-pong

    olas.edit(olas.named("Dentist").id, start="2026-10-09 15:00")   # Ola moves it on his phone
    await rounds(3, start=8)
    assert wall(adas.named("Dentist").entry.start) == "Fri 9 Oct 15:00"
    assert len(olas.calls) + len(adas.calls) == writes + 1          # one update, to Ada's


# ---------------------------------------------------------------- repeating events
async def test_a_weekly_activity_goes_over_with_its_rule_and_a_week_skipped_in_chat_is_skipped_there(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    await say(home, "schedule_event", title="Chatterbox", kind="activity", starts_at="2026-10-06T09:00",
              rrule="FREQ=WEEKLY;BYDAY=TU", participants=["Tobi"])
    await rounds()
    series = theirs.named("Chatterbox for Tobi")
    assert (series.entry.rrule, series.entry.exdates) == ("FREQ=WEEKLY;BYDAY=TU", ())

    await say(home, "modify_event", event="chatterbox", cancel=True, scope="this", occurrence="2026-10-13")
    await rounds(start=1)
    assert theirs.did("update") == [("update", series.id, ["exdates"])]
    assert theirs.named("Chatterbox for Tobi").entry.exdates == (date(2026, 10, 13),)

    await say(home, "modify_event", event="chatterbox", scope="this", occurrence="2026-10-20",
              starts_at="2026-10-21T10:00")          # one week moved: the series skips it, and a one-off is added
    await rounds(2, start=2)
    weekly = theirs.events[series.id].entry
    assert weekly.exdates == (date(2026, 10, 13), date(2026, 10, 20)) and wall(weekly.start) == "Tue 6 Oct 09:00"
    moved = [remote.entry for remote in theirs.events.values() if remote.entry and not remote.entry.rrule]
    assert [(entry.summary, wall(entry.start)) for entry in moved] == [("Chatterbox for Tobi", "Wed 21 Oct 10:00")]


async def test_a_repeating_event_from_the_calendar_repeats_here_and_one_date_deleted_there_is_skipped(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    series = theirs.add("Football", "2026-10-06 17:00", rrule="FREQ=WEEKLY;BYDAY=TU")
    await rounds()
    (event,) = await events()
    assert (event["title"], event["kind"], event["rrule"], event["exdates"]) == (
        "Football", "activity", "FREQ=WEEKLY;BYDAY=TU", [])
    assert [at for at, *_ in await reminders()] == ["Mon 5 Oct 17:00", "Tue 6 Oct 16:00"]     # for tomorrow's

    # The calendar holds a deleted occurrence as an event of its own that points at the series.
    theirs._put(Remote("football-6oct", deleted=True, series_id=series, original=date(2026, 10, 6)))
    await rounds(3, start=1)
    (event,) = await events()
    assert event["exdates"] == [date(2026, 10, 6)] and await reminders() == []
    assert theirs.calls == []                # both sides agree it is skipped: nothing is written back


async def test_one_date_moved_on_the_calendar_is_skipped_in_the_series_and_becomes_its_own_plan(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    series = theirs.add("Football", "2026-10-06 17:00", rrule="FREQ=WEEKLY;BYDAY=TU")
    await rounds()
    moved = theirs.add("Football", "2026-10-14 18:00", series_id=series, original=date(2026, 10, 13),
                       owner=None)
    await rounds(2, start=1)

    weekly, one_off = sorted(await events(), key=lambda event: event["rrule"] is None)
    assert weekly["exdates"] == [date(2026, 10, 13)]
    assert (one_off["title"], wall(one_off["starts_at"]), one_off["rrule"]) == ("Football", "Wed 14 Oct 18:00", None)
    assert theirs.calls == []

    theirs.edit(moved, start="2026-10-15 18:00")            # moved again: the one-off follows, the series is untouched
    await rounds(start=3)
    theirs.remove(moved)                                    # then deleted: the one-off goes, the date stays skipped
    await rounds(2, start=4)
    (weekly,) = await events()
    assert weekly["exdates"] == [date(2026, 10, 13)] and theirs.calls == []
    assert [e["title"] for e in await events("cancelled")] == ["Football"]


async def test_a_changed_occurrence_of_our_own_series_is_not_taken_for_a_second_copy(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    await say(home, "schedule_event", title="Chatterbox", kind="activity", starts_at="2026-10-06T09:00",
              rrule="FREQ=WEEKLY;BYDAY=TU")
    await rounds()
    series = theirs.named("Chatterbox")
    # Moved on the phone. The calendar gives the occurrence its series' mark.
    theirs.add("Chatterbox", "2026-10-13 11:00", series_id=series.id, original=date(2026, 10, 13),
               owner=series.owner)
    await rounds(3, start=1)
    assert theirs.did("delete") == [] and len(await events()) == 2


# ---------------------------------------------------------------- whole days
async def test_an_all_day_event_has_no_reminders_is_in_the_morning_brief_and_goes_back_as_a_date(calendars):
    async with tx() as conn:
        home = await family(conn)
    olas = await connect(home, "Ola", "ola-main", "Ola", calendars=calendars)
    adas = await connect(home, "Ada", "family", "Family", calendars=calendars)
    olas.add("Inset day", "2026-10-09 00:00", minutes=24 * 60, all_day=True)
    await rounds(2)

    (event,) = await events()
    assert event["all_day"] and wall(event["starts_at"]) == "Fri 9 Oct 00:00" and await reminders() == []
    sent = adas.named("Inset day").entry
    assert sent.all_day and (wall(sent.start), wall(sent.end)) == ("Fri 9 Oct 00:00", "Sat 10 Oct 00:00")
    async with tx() as conn:
        household = {"id": home.id, "timezone": "Europe/London"}
        day_before = await jobs._daily_brief(conn, household, london("2026-10-08 07:30"))
        on_the_day = await jobs._daily_brief(conn, household, london("2026-10-09 07:30"))
        upcoming = await calendar.upcoming_lines(conn, home.id, "Europe/London", london("2026-10-09 13:00"),
                                                 london("2026-10-12 13:00"))
    assert day_before == "Tomorrow:\n- All day: Inset day"
    assert on_the_day == "Today, Friday 9 October:\n- All day: Inset day"
    assert upcoming == ["Fri 9 Oct all day Inset day"]         # still "today" after it began at midnight

    olas.edit(olas.named("Inset day").id, start="2026-10-09 10:00", end=london("2026-10-09 12:00"), all_day=False)
    await rounds(2, start=2)                                   # given a time after all: it gets its reminders
    (event,) = await events()
    assert not event["all_day"] and [at for at, *_ in await reminders()] == ["Thu 8 Oct 10:00", "Fri 9 Oct 09:00"]
    assert not adas.named("Inset day").entry.all_day


# ---------------------------------------------------------------- when it goes wrong
async def test_a_fault_at_the_provider_waits_longer_each_time_and_then_carries_on_where_it_was(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    theirs.add("Hairdresser", "2026-10-07 16:00")
    theirs.fail = Unavailable("503")
    await jobs.sync_calendars(NOW)
    held = await connection()
    assert (held["failures"], held["leased_until"], held["status"]) == (1, NOW + calendar_sync.FIRST_WAIT, "active")
    assert "Unavailable" in held["last_error"] and await events() == []

    await jobs.sync_calendars(NOW + timedelta(seconds=10))        # too soon: it is left alone
    assert len(theirs.reads) == 1
    theirs.fail = Unavailable("503")
    await jobs.sync_calendars(NOW + timedelta(seconds=31))
    assert (await connection())["leased_until"] == NOW + timedelta(seconds=31) + 2 * calendar_sync.FIRST_WAIT

    await jobs.sync_calendars(NOW + timedelta(minutes=5))
    after = await connection()
    assert (after["failures"], after["last_error"], after["leased_until"]) == (0, None, None)
    assert [e["title"] for e in await events()] == ["Hairdresser"]


async def test_a_lost_sign_in_stops_the_connection_and_tells_its_owner_once(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, "Ada", calendars=calendars)
    theirs.fail = AuthLost("invalid_grant")
    assert await jobs.sync_calendars(NOW) == 1
    assert await jobs.sync_calendars(NOW + TICK) == 0             # stopped: it is not tried again
    assert (await connection("Ada"))["status"] == "broken" and len(theirs.reads) == 1
    async with tx() as conn:
        (told,) = await fetch_all(conn, "select target, member_id, text from outbox")
        brief = await build_brief(conn, envelope(home, "hi").model_copy(
            update={"member_id": home.members["Ada"], "member_name": "Ada"}), NOW)
    assert (told["target"], told["member_id"]) == ("member", home.members["Ada"])
    assert told["text"] == ("I can no longer reach your Google Calendar, so plans are not being copied between it "
                            "and me. To fix it, send me the word dashboard, open Calendar and tap Connect again.")
    assert "own Google Calendar has stopped working and has to be connected again" in brief

    # Signing in again and picking the same calendar carries on with what was linked before.
    await say(home, "schedule_event", title="Dentist", starts_at="2026-10-09T14:00")
    await connect(home, "Ada")
    await rounds(2, start=2)
    assert (await connection("Ada"))["status"] == "active" and theirs.summaries() == ["Dentist"]


async def test_when_the_calendar_forgets_our_place_everything_is_read_again_without_a_second_copy(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    kept = theirs.add("Hairdresser", "2026-10-07 16:00")
    gone = theirs.add("Optician", "2026-10-12 11:00")
    await say(home, "schedule_event", title="Dentist", starts_at="2026-10-09T14:00")
    await rounds()
    theirs.remove(gone)
    theirs.edit(kept, location="Cut & Dry")
    theirs.forgotten = True
    await rounds(2, start=1)

    assert theirs.reads[1:3] == ["2", None]                         # asked with its token, told no, read it all
    assert [(e["title"], e["location"]) for e in await events()] == [("Hairdresser", "Cut & Dry"), ("Dentist", None)]
    assert [e["title"] for e in await events("cancelled")] == ["Optician"]
    assert len(theirs.did("create")) == 1 and theirs.did("update") == []


async def test_one_event_the_calendar_refuses_does_not_stop_the_others_and_is_tried_again_later(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    theirs.refuse = {"Dentist"}
    await say(home, "schedule_event", title="Dentist", starts_at="2026-10-09T14:00")
    await say(home, "schedule_event", title="Optician", starts_at="2026-10-12T11:00")
    await jobs.sync_calendars(NOW)
    assert theirs.summaries() == ["Optician"]
    held = await connection()
    assert held["failures"] == 1 and held["leased_until"] == NOW + calendar_sync.FIRST_WAIT
    theirs.refuse = set()
    await jobs.sync_calendars(NOW + timedelta(minutes=1))
    assert theirs.summaries() == ["Dentist", "Optician"] and (await connection())["failures"] == 0


async def test_an_event_that_cannot_be_held_here_is_left_out_and_the_rest_come_in(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    theirs._put(Remote("odd", skipped="it repeats in a way that cannot be followed here"))
    theirs.add("Every minute", "2026-10-07 16:00", rrule="FREQ=MINUTELY")     # the calendar service refuses this one
    theirs.add("Hairdresser", "2026-10-07 16:00")
    await rounds(2)
    assert [e["title"] for e in await events()] == ["Hairdresser"]
    assert (await connection())["failures"] == 0


# ---------------------------------------------------------------- connecting, and the sign-in at rest
async def test_the_sign_in_is_stored_encrypted_survives_a_new_key_in_front_and_is_never_exported(calendars):
    async with tx() as conn:
        home = await family(conn)
    await connect(home)
    settings = get_settings()
    stored = (await connection())["secret"]
    assert "refresh-Ola" not in stored and calendar_sync.unseal(settings, stored) == {"refresh_token": "refresh-Ola"}

    rotated = settings.model_copy(update={"calendar_secret_key": f"{crypto.new_key()},{settings.calendar_secret_key}"})
    assert calendar_sync.unseal(rotated, stored) == {"refresh_token": "refresh-Ola"}
    assert calendar_sync.unseal(rotated, calendar_sync.seal(rotated, refresh_token="new")) == {"refresh_token": "new"}
    with pytest.raises(crypto.Unreadable):
        calendar_sync.unseal(settings.model_copy(update={"calendar_secret_key": crypto.new_key()}), stored)
    with pytest.raises(ValueError, match="CALENDAR_SECRET_KEY must be a Fernet key"):
        type(settings)(calendar_secret_key="not-a-key")

    async with tx() as conn:
        exported = await households.export(conn, home.id)
    (row,) = exported["calendar_connections"]
    assert "secret" not in row and "sync_token" not in row and row["calendar_name"] == "Family"
    assert "refresh-Ola" not in str(exported) and stored not in str(exported)


async def test_a_sign_in_the_key_no_longer_opens_stops_the_connection_like_a_lost_one(calendars, monkeypatch):
    async with tx() as conn:
        home = await family(conn)
        await connect_in(conn, home)
        await execute(conn, "update calendar_connections set secret = :other",
                      other=crypto.seal(crypto.new_key(), "{}"))
    assert await jobs.sync_calendars(NOW) == 1
    assert (await connection())["status"] == "broken"


async def connect_in(conn, home, who="Ola"):
    await calendar_sync.begin(conn, home.id, home.members[who], "google", f"{who.lower()}@example.test",
                              calendar_sync.seal(get_settings(), refresh_token="r"), [RemoteCalendar("family", "Family")])
    await calendar_sync.choose(Recorder(ctx_for(conn, home, who, now=NOW)), "family")


async def test_one_calendar_is_shared_once_and_a_different_one_starts_from_nothing(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    theirs.add("Hairdresser", "2026-10-07 16:00")
    await rounds()
    async with tx() as conn:
        await calendar_sync.begin(conn, home.id, home.members["Ada"], "google", "ada@example.test",
                                  calendar_sync.seal(get_settings(), refresh_token="r"),
                                  [RemoteCalendar("family", "Family"), RemoteCalendar("ada-main", "Ada")])
        with pytest.raises(calendar.ToolError, match="Ola already shares that calendar with the household"):
            await calendar_sync.choose(Recorder(ctx_for(conn, home, "Ada", now=NOW)), "family")
        with pytest.raises(calendar.ToolError, match="pick one of the calendars listed"):
            await calendar_sync.choose(Recorder(ctx_for(conn, home, "Ada", now=NOW)), "somebody-elses")
        await calendar_sync.choose(Recorder(ctx_for(conn, home, "Ada", now=NOW)), "ada-main")
        assert await fetch_val(conn, "select count(*) from calendar_connections where status = 'active'") == 2

    # Ola signs in again and picks another calendar: the old one's links and place are dropped.
    async with tx() as conn:
        await calendar_sync.begin(conn, home.id, home.ola, "google", "ola@example.test",
                                  calendar_sync.seal(get_settings(), refresh_token="r2"),
                                  [RemoteCalendar("family", "Family"), RemoteCalendar("work", "Work")])
        await calendar_sync.choose(Recorder(ctx_for(conn, home, now=NOW)), "work")
        mine = await fetch_one(conn, "select * from calendar_connections where member_id = :m", m=home.ola)
        assert (mine["calendar_name"], mine["sync_token"], mine["last_synced_at"]) == ("Work", None, None)
        assert await fetch_val(conn, "select count(*) from calendar_links where connection_id = :c", c=mine["id"]) == 0
        logged = await fetch_all(conn, "select result, inverse from agent_actions where tool like 'calendar%' "
                                       "and source <> 'calendar'")
    assert logged == []      # these went through a bare Recorder; the page logs them (tests/unit/test_dashboard.py)


async def test_links_to_one_offs_long_over_are_dropped_and_nothing_is_made_again_for_them(calendars):
    async with tx() as conn:
        home = await family(conn)
    theirs = await connect(home, calendars=calendars)
    await say(home, "schedule_event", title="Dentist", starts_at="2026-10-09T14:00")
    await rounds()
    later = london("2026-11-20 12:00")
    await jobs.sync_calendars(later)
    await jobs.sync_calendars(later + TICK)
    async with tx() as conn:
        assert await fetch_val(conn, "select count(*) from calendar_links") == 0
    assert len(theirs.calls) == 1 and theirs.summaries() == ["Dentist"]     # still on the calendar, just not followed
