"""Calendar tools and service: events, reminder planning, recurrence, matching and undo (spec 9 and 9.2)."""
from datetime import UTC, date, datetime

import pytest

from app.agent.prompt import build_brief
from app.agent.resolve import rank_events
from app.agent.tools import run_tool
from app.core.envelope import Envelope
from app.db import fetch_all, tx
from tests.helpers import add_member, ctx_for, london, seed_home, wall

NOW = london("2026-10-05 12:00")     # a Monday


async def family(conn):
    home = await seed_home(conn)
    await add_member(conn, home, "Ada", telegram_id="1002")
    await add_member(conn, home, "Tobi", role="child")
    return home


async def do(conn, home, tool, *, now=NOW, **args):
    return await run_tool(tool, args, ctx_for(conn, home, "Ola", now=now))


async def ok(conn, home, tool, **args):
    result, is_error = await do(conn, home, tool, **args)
    assert not is_error, result
    return result


async def gp(conn, home, **overrides):
    return await ok(conn, home, "schedule_event", **{
        "title": "GP", "starts_at": "2026-10-07T10:30", "participants": ["Ada"], "location": "Hurley Clinic",
        **overrides})


async def chatterbox(conn, home, **overrides):
    return await ok(conn, home, "schedule_event", **{
        "title": "Chatterbox", "kind": "activity", "starts_at": "2026-10-05T09:00", "rrule": "FREQ=WEEKLY;BYDAY=TU",
        "participants": ["Tobi"], "location": "library", **overrides})


async def events(conn):
    return await fetch_all(conn, "select * from events order by created_at, id")


async def reminders(conn, status="scheduled"):
    """(local fire time, text, target, member, urgency) of each reminder, soonest first."""
    rows = await fetch_all(
        conn,
        """select r.fire_at, r.text, r.target, m.name, r.urgency from reminders r
           left join members m on m.id = r.member_id where r.status = :status order by r.fire_at""", status=status)
    return [(wall(r["fire_at"]), r["text"], r["target"], r["name"], r["urgency"]) for r in rows]


async def snapshot(conn):
    return (await fetch_all(conn, "select * from events order by id"),
            await fetch_all(conn, "select * from reminders order by id"))


# ---------------------------------------------------------------- scheduling
async def test_an_appointment_gets_its_row_and_a_reminder_the_day_before_and_an_hour_before():
    async with tx() as conn:
        home = await family(conn)
        result = await gp(conn, home)
        (event,) = await events(conn)
        assert (event["title"], event["kind"], wall(event["starts_at"]), event["location"], event["rrule"]) == (
            "GP", "appointment", "Wed 7 Oct 10:30", "Hurley Clinic", None)
        assert [str(p) for p in event["participant_ids"]] == [home.members["Ada"]]
        assert event["created_by"] == home.ola and event["remind_before_minutes"] == [1440, 60]
        assert await reminders(conn) == [
            ("Tue 6 Oct 10:30", "GP tomorrow at 10:30, Hurley Clinic", "member", "Ada", "normal"),
            ("Wed 7 Oct 09:30", "GP today at 10:30, Hurley Clinic", "member", "Ada", "normal"),
        ]
        assert "OK: GP for Ada on Wed 7 Oct at 10:30, Hurley Clinic" in result
        assert "reminders at Tue 6 Oct 10:30, Wed 7 Oct 09:30" in result


@pytest.mark.parametrize("participants,target,member,text", [
    (["Tobi"], "household", None, "Swimming for Tobi tomorrow at 10:30"),      # a child takes part
    (["me", "Ada"], "household", None, "Swimming for Ola and Ada tomorrow at 10:30"),   # more than one adult
    (["Ada"], "member", "Ada", "Swimming tomorrow at 10:30"),
    (["me"], "member", "Ola", "Swimming tomorrow at 10:30"),
    ([], "member", "Ola", "Swimming tomorrow at 10:30"),                       # nobody named: whoever booked it
    (["us"], "household", None, "Swimming for Ola and Ada tomorrow at 10:30"),
    (["the kids"], "household", None, "Swimming for Tobi tomorrow at 10:30"),
])
async def test_reminders_go_to_the_household_or_the_one_adult_taking_part(participants, target, member, text):
    async with tx() as conn:
        home = await family(conn)
        await ok(conn, home, "schedule_event", title="Swimming", starts_at="2026-10-07T10:30",
                 participants=participants)
        assert (await reminders(conn))[0] == ("Tue 6 Oct 10:30", text, target, member, "normal")


async def test_a_naive_time_is_household_time_on_both_sides_of_the_clock_change_and_an_offset_is_kept():
    async with tx() as conn:
        home = await family(conn)
        await gp(conn, home, title="before", starts_at="2026-10-21T10:30")
        await gp(conn, home, title="after", starts_at="2026-10-28T10:30")
        await gp(conn, home, title="abroad", starts_at="2026-10-28T10:30:00+01:00")
        starts = {e["title"]: e["starts_at"] for e in await events(conn)}
    assert starts == {
        "before": datetime(2026, 10, 21, 9, 30, tzinfo=UTC),      # BST
        "after": datetime(2026, 10, 28, 10, 30, tzinfo=UTC),      # GMT
        "abroad": datetime(2026, 10, 28, 9, 30, tzinfo=UTC),
    }


@pytest.mark.parametrize("starts_at,leads,expected", [
    # 06:30: the day-before lands in quiet hours and is left to the morning brief; the hour-before
    # would be held until 07:00, after the event began, so it goes out on time.
    ("2026-10-08T06:30", [1440, 60], [("Thu 8 Oct 05:30", "Run today at 06:30", "high")]),
    # 07:30: the hour-before is held until 07:00, still before the event, so it stays an ordinary send.
    ("2026-10-08T07:30", [1440, 60], [("Wed 7 Oct 07:30", "Run tomorrow at 07:30", "normal"),
                                      ("Thu 8 Oct 06:30", "Run today at 07:30", "normal")]),
    # Ten hours before 08:00 is 22:00 the night before; it will be read at 07:00, so it says "today".
    ("2026-10-08T08:00", [600], [("Wed 7 Oct 22:00", "Run today at 08:00", "normal")]),
])
async def test_reminders_that_land_in_quiet_hours(starts_at, leads, expected):
    async with tx() as conn:
        home = await family(conn)
        await ok(conn, home, "schedule_event", title="Run", starts_at=starts_at, remind_before_minutes=leads)
        assert [(at, text, urgency) for at, text, _, _, urgency in await reminders(conn)] == expected


async def test_reminders_already_in_the_past_are_not_created_and_a_past_event_is_refused():
    async with tx() as conn:
        home = await family(conn)
        result = await ok(conn, home, "schedule_event", title="Soon", starts_at="2026-10-05T12:30")
        assert "no reminders" in result and await reminders(conn) == []
        await ok(conn, home, "schedule_event", title="Tomorrow", starts_at="2026-10-06T09:00")
        assert [at for at, *_ in await reminders(conn)] == ["Tue 6 Oct 08:00"]

        result, is_error = await do(conn, home, "schedule_event", title="Gone", starts_at="2026-10-05T11:00")
        assert is_error and "already passed" in result and "Mon 5 Oct 2026 12:00" in result
        assert [e["title"] for e in await events(conn)] == ["Soon", "Tomorrow"]


async def test_bad_arguments_are_errors_the_model_can_recover_from():
    async with tx() as conn:
        home = await family(conn)
        for bad in [{"rrule": "every tuesday"}, {"ends_at": "2026-10-07T10:00"}, {"remind_before_minutes": [99999]},
                    {"title": "  "}, {"starts_at": "next wednesday"}]:
            result, is_error = await do(conn, home, "schedule_event", **{
                "title": "GP", "starts_at": "2026-10-07T10:30", **bad})
            assert is_error and result.startswith("ERROR:"), bad
        assert await events(conn) == []


async def test_scheduling_the_same_thing_twice_keeps_one_event_and_an_unknown_name_does_not_block_it():
    async with tx() as conn:
        home = await family(conn)
        await gp(conn, home)
        assert "already on the calendar" in await gp(conn, home)
        assert len(await events(conn)) == 1 and len(await reminders(conn)) == 2

        result = await ok(conn, home, "schedule_event", title="Football", starts_at="2026-10-10T10:00",
                          participants=["Zed", "Tobi"])
        assert "nobody called 'Zed'" in result
        assert [str(p) for p in (await events(conn))[1]["participant_ids"]] == [home.members["Tobi"]]


# ---------------------------------------------------------------- moving, editing, cancelling, undo
async def test_moving_an_event_regenerates_its_reminders_and_undo_restores_everything_exactly():
    async with tx() as conn:
        home = await family(conn)
        await gp(conn, home)
        before = await snapshot(conn)

        result = await ok(conn, home, "modify_event", event="Ada's GP appointment", starts_at="2026-10-07T11:00")
        assert "OK: now GP for Ada on Wed 7 Oct at 11:00, Hurley Clinic" in result
        (event,) = await events(conn)
        assert wall(event["starts_at"]) == "Wed 7 Oct 11:00"
        assert [(at, text) for at, text, *_ in await reminders(conn)] == [
            ("Tue 6 Oct 11:00", "GP tomorrow at 11:00, Hurley Clinic"),
            ("Wed 7 Oct 10:00", "GP today at 11:00, Hurley Clinic"),
        ]
        assert len(await fetch_all(conn, "select 1 from reminders")) == 2      # nothing left from 10:30

        result = await ok(conn, home, "undo_last")
        assert "undid modify_event" in result
        assert await snapshot(conn) == before


async def test_an_edit_that_keeps_the_time_rewrites_the_same_reminder_rows():
    async with tx() as conn:
        home = await family(conn)
        await gp(conn, home, ends_at="2026-10-07T11:00")
        before = await snapshot(conn)
        await ok(conn, home, "modify_event", event="GP", title="Doctor", location="Riverside Surgery",
                 participants=["Tobi"])
        after = await snapshot(conn)
        assert [r["id"] for r in after[1]] == [r["id"] for r in before[1]]
        assert [(text, target) for _, text, target, _, _ in await reminders(conn)] == [
            ("Doctor for Tobi tomorrow at 10:30, Riverside Surgery", "household"),
            ("Doctor for Tobi today at 10:30, Riverside Surgery", "household"),
        ]
        await ok(conn, home, "undo_last")
        assert await snapshot(conn) == before


async def test_moving_keeps_the_duration():
    async with tx() as conn:
        home = await family(conn)
        await gp(conn, home, ends_at="2026-10-07T11:15")
        await ok(conn, home, "modify_event", event="GP", starts_at="2026-10-08T14:00")
        (event,) = await events(conn)
        assert (wall(event["starts_at"]), wall(event["ends_at"])) == ("Thu 8 Oct 14:00", "Thu 8 Oct 14:45")
        result, is_error = await do(conn, home, "modify_event", event="GP", ends_at="2026-10-08T13:00")
        assert is_error and "end must be after" in result


async def test_cancelling_cancels_the_reminders_and_undo_brings_both_back():
    async with tx() as conn:
        home = await family(conn)
        await gp(conn, home)
        before = await snapshot(conn)
        assert "OK: GP for Ada cancelled" in await ok(conn, home, "modify_event", event="GP", cancel=True)
        assert (await events(conn))[0]["status"] == "cancelled"
        assert await reminders(conn) == [] and len(await reminders(conn, "cancelled")) == 2

        result, is_error = await do(conn, home, "modify_event", event="GP", cancel=True)
        assert is_error and "no upcoming event matches" in result       # it is no longer on the calendar

        await ok(conn, home, "undo_last")
        assert await snapshot(conn) == before


async def test_undoing_a_new_event_removes_it_and_its_reminders():
    async with tx() as conn:
        home = await family(conn)
        await gp(conn, home)
        await ok(conn, home, "undo_last")
        assert await snapshot(conn) == ([], [])


async def test_a_change_with_nothing_to_change_is_an_error():
    async with tx() as conn:
        home = await family(conn)
        await gp(conn, home)
        result, is_error = await do(conn, home, "modify_event", event="GP")
        assert is_error and "say what to change" in result


# ---------------------------------------------------------------- recurring events
async def test_a_recurring_event_starts_on_its_first_real_occurrence_and_only_has_reminders_for_48_hours():
    async with tx() as conn:
        home = await family(conn)
        result = await chatterbox(conn, home)          # "starts" on Monday 5th, but the rule says Tuesdays
        (event,) = await events(conn)
        assert (wall(event["starts_at"]), event["rrule"]) == ("Tue 6 Oct 09:00", "FREQ=WEEKLY;BYDAY=TU")
        assert "repeating (FREQ=WEEKLY;BYDAY=TU)" in result and "1 day and 1 hour before each one" in result
        # Monday 09:00 has passed; next week's rows come from the hourly recurrence job.
        assert await reminders(conn) == [
            ("Tue 6 Oct 08:00", "Chatterbox for Tobi today at 09:00, library", "household", None, "normal")]


async def test_skipping_one_occurrence_adds_an_exception_date_and_leaves_the_series():
    async with tx() as conn:
        home = await family(conn)
        await chatterbox(conn, home)
        before = await snapshot(conn)
        result = await ok(conn, home, "modify_event", event="chatterbox", cancel=True)
        assert "Chatterbox for Tobi on Tue 6 Oct at 09:00, library cancelled; the other dates stand" in result
        (event,) = await events(conn)
        assert (event["status"], event["exdates"]) == ("active", [date(2026, 10, 6)])
        assert await reminders(conn) == [] and len(await reminders(conn, "cancelled")) == 1
        upcoming = await ok(conn, home, "list_upcoming", days=14)
        assert "Tue 6 Oct" not in upcoming and "Tue 13 Oct 09:00 Chatterbox (Tobi), library [repeats]" in upcoming

        await ok(conn, home, "undo_last")
        assert await snapshot(conn) == before


async def test_moving_one_occurrence_skips_its_date_and_adds_a_one_off_event():
    async with tx() as conn:
        home = await family(conn)
        await chatterbox(conn, home, ends_at="2026-10-05T10:00")
        before = await snapshot(conn)
        result = await ok(conn, home, "modify_event", event="Chatterbox", occurrence="2026-10-13",
                          starts_at="2026-10-14T10:00")
        assert "OK: Chatterbox for Tobi on Wed 14 Oct at 10:00, library" in result
        assert "only Tue 13 Oct changed" in result
        series, moved = await events(conn)
        assert series["exdates"] == [date(2026, 10, 13)] and series["rrule"]
        assert (moved["rrule"], wall(moved["starts_at"]), wall(moved["ends_at"]), moved["location"]) == (
            None, "Wed 14 Oct 10:00", "Wed 14 Oct 11:00", "library")
        assert moved["participant_ids"] == series["participant_ids"]
        assert [at for at, *_ in await reminders(conn)] == ["Tue 6 Oct 08:00", "Tue 13 Oct 10:00", "Wed 14 Oct 09:00"]

        result, is_error = await do(conn, home, "modify_event", event="Chatterbox", occurrence="2026-10-21",
                                    cancel=True)
        assert is_error and "no occurrence on Wed 21 Oct" in result

        await ok(conn, home, "undo_last")
        assert await snapshot(conn) == before


async def test_changing_the_whole_series_moves_every_occurrence_and_cancelling_it_ends_it():
    async with tx() as conn:
        home = await family(conn)
        await chatterbox(conn, home)
        before = await snapshot(conn)
        await ok(conn, home, "modify_event", event="Chatterbox", scope="all", starts_at="2026-10-06T10:00")
        (event,) = await events(conn)
        assert wall(event["starts_at"]) == "Tue 6 Oct 10:00"
        assert [(at, text) for at, text, *_ in await reminders(conn)] == [
            ("Tue 6 Oct 09:00", "Chatterbox for Tobi today at 10:00, library")]
        await ok(conn, home, "undo_last")
        assert await snapshot(conn) == before

        result = await ok(conn, home, "modify_event", event="Chatterbox", scope="all", cancel=True)
        assert "the whole series" in result
        assert (await events(conn))[0]["status"] == "cancelled" and await reminders(conn) == []
        assert await ok(conn, home, "list_upcoming", days=30) == "Nothing in the next 30 days."


# ---------------------------------------------------------------- finding the event
async def test_events_are_matched_on_title_and_participants_within_60_days_soonest_first():
    async with tx() as conn:
        home = await family(conn)
        await gp(conn, home)
        await ok(conn, home, "schedule_event", title="Dentist", starts_at="2026-11-20T15:00", participants=["Tobi"])
        await ok(conn, home, "schedule_event", title="Dentist", starts_at="2026-10-09T15:00", participants=["me"])
        await ok(conn, home, "schedule_event", title="School play", starts_at="2026-12-18T18:00")
        await chatterbox(conn, home)

        async def best(text):
            top = (await rank_events(conn, home.id, text, NOW))[0]
            return top.title, wall(top.next_start)

        assert await best("Ada's GP appointment") == ("GP", "Wed 7 Oct 10:30")
        assert await best("the dentist") == ("Dentist", "Fri 9 Oct 15:00")         # a tie: the sooner one
        assert await best("ada") == ("GP", "Wed 7 Oct 10:30")                      # by participant
        assert await best("chatterbox") == ("Chatterbox", "Tue 6 Oct 09:00")
        assert "School play" not in {c.title for c in await rank_events(conn, home.id, "school play", NOW)}

        for unknown in ("school play", "swimming lesson"):
            result, is_error = await do(conn, home, "modify_event", event=unknown, cancel=True)
            assert is_error and "no upcoming event matches" in result and "GP (Wed 7 Oct)" in result
        assert all(e["status"] == "active" for e in await events(conn))


# ---------------------------------------------------------------- standalone reminders
async def test_a_one_off_reminder_goes_to_the_person_the_household_or_a_named_member():
    async with tx() as conn:
        home = await family(conn)
        result = await ok(conn, home, "set_reminder", text="call the landlord", fire_at="2026-10-09T09:00")
        assert result == "OK: reminder set for Fri 9 Oct 09:00: call the landlord"
        await ok(conn, home, "set_reminder", text="bins out", fire_at="2026-10-09T10:00", target="household")
        await ok(conn, home, "set_reminder", text="post the parcel", fire_at="2026-10-09T11:00", target="Ada")
        assert await reminders(conn) == [
            ("Fri 9 Oct 09:00", "call the landlord", "member", "Ola", "normal"),
            ("Fri 9 Oct 10:00", "bins out", "household", None, "normal"),
            ("Fri 9 Oct 11:00", "post the parcel", "member", "Ada", "normal"),
        ]
        assert all(r["event_id"] is None for r in await fetch_all(conn, "select event_id from reminders"))

        await ok(conn, home, "set_reminder", text="call the landlord", fire_at="2026-10-09T09:00")    # said twice
        assert len(await reminders(conn)) == 3
        await ok(conn, home, "undo_last")
        assert [text for _, text, *_ in await reminders(conn)] == ["call the landlord", "bins out"]


async def test_a_reminder_set_inside_quiet_hours_or_marked_urgent_is_sent_regardless():
    async with tx() as conn:
        home = await family(conn)
        await ok(conn, home, "set_reminder", text="take the bins out", fire_at="2026-10-05T23:00")
        await ok(conn, home, "set_reminder", text="ring the school", fire_at="2026-10-06T09:00", urgent=True)
        await ok(conn, home, "set_reminder", text="water the plants", fire_at="2026-10-06T10:00")
        assert [(text, urgency) for _, text, _, _, urgency in await reminders(conn)] == [
            ("take the bins out", "high"), ("ring the school", "high"), ("water the plants", "normal")]


async def test_repeating_reminders_start_at_their_first_occurrence():
    async with tx() as conn:
        home = await family(conn)
        result = await ok(conn, home, "set_reminder", text="bins out", rrule="FREQ=WEEKLY;BYDAY=SU;BYHOUR=18",
                          target="household")
        assert "repeating reminder (FREQ=WEEKLY;BYDAY=SU;BYHOUR=18), first on Sun 11 Oct 18:00" in result
        await ok(conn, home, "set_reminder", text="vitamins", rrule="FREQ=DAILY", fire_at="2026-10-06T08:00")
        rows = await fetch_all(conn, "select text, fire_at, rrule, target from reminders order by fire_at")
        assert [(r["text"], wall(r["fire_at"]), r["rrule"], r["target"]) for r in rows] == [
            ("vitamins", "Tue 6 Oct 08:00", "FREQ=DAILY", "member"),
            ("bins out", "Sun 11 Oct 18:00", "FREQ=WEEKLY;BYDAY=SU;BYHOUR=18", "household"),
        ]


async def test_reminders_that_cannot_work_are_errors():
    async with tx() as conn:
        home = await family(conn)
        for bad, why in [
            ({"fire_at": "2026-10-05T11:00"}, "already passed"),
            ({}, "give fire_at"),
            ({"rrule": "FREQ=DAILY;COUNT=5", "fire_at": "2026-10-06T08:00"}, "UNTIL"),
            ({"rrule": "FREQ=WEEKLY;BYDAY=SU"}, "what time of day"),
            ({"rrule": "sometimes", "fire_at": "2026-10-06T08:00"}, "not valid"),
            ({"fire_at": "2026-10-06T08:00", "target": "Zed"}, "nobody called 'Zed'"),
            ({"fire_at": "2026-10-06T08:00", "text": " "}, "what the reminder is for"),
        ]:
            result, is_error = await do(conn, home, "set_reminder", **{"text": "x", **bad})
            assert is_error and why in result, (bad, result)
        assert await fetch_all(conn, "select 1 from reminders") == []
        assert await fetch_all(conn, "select 1 from agent_actions") == []


# ---------------------------------------------------------------- reading
async def test_list_upcoming_expands_repeats_includes_reminders_and_filters_by_person():
    async with tx() as conn:
        home = await family(conn)
        assert await ok(conn, home, "list_upcoming") == "Nothing in the next 7 days."
        await chatterbox(conn, home)
        await gp(conn, home)
        await ok(conn, home, "set_reminder", text="call the landlord", fire_at="2026-10-09T09:00")
        await ok(conn, home, "set_reminder", text="bins out", rrule="FREQ=WEEKLY;BYDAY=SU;BYHOUR=18",
                 target="household")

        assert (await ok(conn, home, "list_upcoming", days=9)).splitlines() == [
            "Tue 6 Oct 09:00 Chatterbox (Tobi), library [repeats]",
            "Wed 7 Oct 10:30 GP (Ada), Hurley Clinic",
            "Fri 9 Oct 09:00 reminder: call the landlord (Ola)",
            "Sun 11 Oct 18:00 reminder: bins out (everyone) [repeats]",
            "Tue 13 Oct 09:00 Chatterbox (Tobi), library [repeats]",
        ]
        assert (await ok(conn, home, "list_upcoming", days=9, member="Ada")).splitlines() == [
            "Wed 7 Oct 10:30 GP (Ada), Hurley Clinic",
            "Sun 11 Oct 18:00 reminder: bins out (everyone) [repeats]",
        ]
        assert await fetch_all(conn, "select 1 from agent_actions where tool = 'list_upcoming'") == []


async def test_the_household_brief_lists_the_next_seven_days():
    async with tx() as conn:
        home = await family(conn)
        await chatterbox(conn, home)
        await gp(conn, home)
        envelope = Envelope(household_id=home.id, member_id=home.ola, member_name="Ola", thread_id=None,
                            message_ids=[], channel=None, scope="dm", text="hi", received_at=NOW)
        brief = await build_brief(conn, envelope, NOW)
    assert "Now: Monday 5 Oct 2026 12:00 (Europe/London)" in brief
    assert "Next 7 days: Tue 6 Oct 09:00 Chatterbox (Tobi), library; Wed 7 Oct 10:30 GP (Ada), Hurley Clinic" in brief
