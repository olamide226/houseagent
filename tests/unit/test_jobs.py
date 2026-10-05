"""Worker jobs on a controlled clock: reminders, recurrence, the daily brief and the weekly digest.

Covers the acceptance items "reminders fire within 60 s of fire_at and hold during quiet hours
unless urgent" and "every job may run twice", and the milestone exit: a weekly activity and a
one-off appointment remind at the right local time on both sides of the 25 Oct 2026 clock change.
"""
import asyncio
import time
from datetime import date, timedelta

import pytest

from app.agent.loop import LoopRuntime
from app.agent.tools import run_tool
from app.config import get_settings
from app.core.envelope import Channel
from app.db import execute, fetch_all, tx
from app.pipeline import router
from app.worker import jobs
from tests.helpers import (
    FakeAdapter,
    FakeLLM,
    add_item,
    add_member,
    ctx_for,
    london,
    say,
    seed_home,
    tg_update,
    wall,
)

NOW = london("2026-10-05 12:00")     # a Monday


async def family(conn):
    home = await seed_home(conn)                          # Ola, Telegram 1001
    await add_member(conn, home, "Ada", telegram_id="1002")
    await add_member(conn, home, "Tobi", role="child")
    return home


async def tool(conn, home, name, *, now=NOW, **args):
    result, is_error = await run_tool(name, args, ctx_for(conn, home, "Ola", now=now))
    assert not is_error, result
    return result


async def rows(sql, **params):
    async with tx() as conn:
        return await fetch_all(conn, sql, **params)


async def send(now, adapter):
    while await router.dispatch_due({Channel.telegram: adapter}, now=now):
        pass


async def run_worker(start, end, adapter, step=timedelta(minutes=30)):
    """What the worker does between two moments: recurrence on the hour, reminders and the
    outbox at every step. Returns (when, chat, text) for every send."""
    sends, now = [], start
    while now <= end:
        if now.minute == 0:
            await jobs.expand_recurrence(now)
        await jobs.fire_reminders(now)
        already = len(adapter.sent)
        await send(now, adapter)
        sends += [(now, chat, text) for chat, text, _ in adapter.sent[already:]]
        now += step
    return sends


# ---------------------------------------------------------------- the clock change
async def test_chatterbox_and_the_gp_remind_at_the_right_local_time_across_the_25_october_clock_change():
    created = london("2026-10-19 08:00")
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "schedule_event", now=created, title="Chatterbox", kind="activity",
                   starts_at="2026-10-06T09:00", rrule="FREQ=WEEKLY;BYDAY=TU", participants=["Tobi"],
                   location="library")
        for day in ("2026-10-21", "2026-10-28"):
            await tool(conn, home, "schedule_event", now=created, title="GP", starts_at=f"{day}T10:30",
                       participants=["Ada"], location="Hurley Clinic")

    adapter = FakeAdapter()
    sends = await run_worker(created, london("2026-10-28 12:00"), adapter)

    both, ada = ["1001", "1002"], ["1002"]          # Chatterbox is for a child: every adult. The GP is Ada's.
    expected = [
        ("Mon 19 Oct 09:00", both, "Chatterbox for Tobi tomorrow at 09:00, library"),
        ("Tue 20 Oct 08:00", both, "Chatterbox for Tobi today at 09:00, library"),
        ("Tue 20 Oct 10:30", ada, "GP tomorrow at 10:30, Hurley Clinic"),
        ("Wed 21 Oct 09:30", ada, "GP today at 10:30, Hurley Clinic"),
        # The clocks go back on Sunday 25 October.
        ("Mon 26 Oct 09:00", both, "Chatterbox for Tobi tomorrow at 09:00, library"),
        ("Tue 27 Oct 08:00", both, "Chatterbox for Tobi today at 09:00, library"),
        ("Tue 27 Oct 10:30", ada, "GP tomorrow at 10:30, Hurley Clinic"),
        ("Wed 28 Oct 09:30", ada, "GP today at 10:30, Hurley Clinic"),
    ]
    assert sorted((wall(at), chat, text) for at, chat, text in sends) == sorted(
        (at, chat, text) for at, chats, text in expected for chat in chats)
    # Same wall-clock time, a different instant: an hour later in UTC once BST has ended.
    hour_before = {wall(at): at.hour for at, _, text in sends if "today" in text}
    assert hour_before == {"Tue 20 Oct 08:00": 7, "Wed 21 Oct 09:30": 8, "Tue 27 Oct 08:00": 8, "Wed 28 Oct 09:30": 9}
    assert await rows("select 1 from reminders where status = 'scheduled' and fire_at <= :t",
                      t=london("2026-10-28 12:00")) == []


# ---------------------------------------------------------------- reminders
async def test_a_reminder_fires_at_its_time_not_before_and_is_sent_within_a_minute():
    fire_at = london("2026-10-07 09:30")
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "set_reminder", text="call the landlord", fire_at="2026-10-07T09:30")
    adapter = FakeAdapter()

    assert await jobs.fire_reminders(fire_at - timedelta(seconds=1)) == 0
    assert await rows("select 1 from outbox") == []

    # The slowest path: the reminders tick has just passed, then the outbox tick has too.
    queued_at = fire_at + timedelta(seconds=jobs.REMINDERS_SECONDS)
    sent_at = queued_at + timedelta(seconds=jobs.POLL_SECONDS)
    assert sent_at - fire_at < timedelta(seconds=60)
    assert await jobs.fire_reminders(queued_at) == 1
    await send(sent_at, adapter)

    assert adapter.sent == [("1001", "call the landlord", None)]
    (reminder,) = await rows("select status, sent_at from reminders")
    assert (reminder["status"], reminder["sent_at"]) == ("sent", queued_at)
    (queued,) = await rows("select status, send_after, dedupe_key from outbox")
    assert (queued["status"], queued["send_after"]) == ("sent", queued_at)
    assert queued["dedupe_key"].startswith("reminder:") and queued["dedupe_key"].endswith(fire_at.isoformat())


async def test_the_reminders_job_run_twice_sends_once():
    fire_at = london("2026-10-07 09:30")
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "set_reminder", text="call the landlord", fire_at="2026-10-07T09:30")
    adapter = FakeAdapter()
    assert await jobs.fire_reminders(fire_at) == 1
    assert await jobs.fire_reminders(fire_at) == 0
    await send(fire_at, adapter)

    # A crash between queuing the send and marking the reminder: the dedupe key still holds.
    async with tx() as conn:
        await execute(conn, "update reminders set status = 'scheduled', sent_at = null")
    assert await jobs.fire_reminders(fire_at + timedelta(seconds=15)) == 0
    await send(fire_at + timedelta(seconds=17), adapter)

    assert adapter.sent == [("1001", "call the landlord", None)]
    assert len(await rows("select 1 from outbox")) == 1
    assert [r["status"] for r in await rows("select status from reminders")] == ["sent"]


async def test_two_workers_running_the_job_at_once_queue_each_reminder_once():
    fire_at = london("2026-10-07 09:30")
    async with tx() as conn:
        home = await family(conn)
        for n in range(6):
            await tool(conn, home, "set_reminder", text=f"thing {n}", fire_at="2026-10-07T09:30")
    queued = await asyncio.gather(*(jobs.fire_reminders(fire_at) for _ in range(4)))
    assert sum(queued) == 6
    assert sorted(r["text"] for r in await rows("select text from outbox")) == [f"thing {n}" for n in range(6)]


async def test_a_reminder_another_worker_has_claimed_is_skipped_not_waited_for():
    fire_at = london("2026-10-07 09:30")
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "set_reminder", text="call the landlord", fire_at="2026-10-07T09:30")
    async with tx() as other_worker:                     # mid-transaction on the same row
        await fetch_all(other_worker, "select id from reminders for update")
        assert await asyncio.wait_for(jobs.fire_reminders(fire_at), timeout=3) == 0
    assert await jobs.fire_reminders(fire_at) == 1


async def test_reminders_hold_during_quiet_hours_unless_urgent():
    night, morning = london("2026-10-06 22:00"), london("2026-10-07 07:00")
    async with tx() as conn:
        home = await family(conn)
        for text, urgency in [("not urgent", "normal"), ("urgent", "high")]:
            await execute(
                conn, "insert into reminders (household_id, target, member_id, text, fire_at, urgency) "
                      "values (:h, 'member', :m, :text, :at, :urgency)",
                h=home.id, m=home.ola, text=text, at=night, urgency=urgency)
    adapter = FakeAdapter()

    assert await jobs.fire_reminders(night) == 2           # both fire on time; the router holds one
    await send(night, adapter)
    assert adapter.sent == [("1001", "urgent", None)]
    (held,) = await rows("select status, send_after from outbox where text = 'not urgent'")
    assert (held["status"], held["send_after"]) == ("pending", morning)

    await send(morning - timedelta(seconds=1), adapter)
    assert len(adapter.sent) == 1
    await send(morning, adapter)
    assert adapter.sent[1] == ("1001", "not urgent", None)


async def test_an_early_event_is_reminded_before_it_starts_despite_quiet_hours():
    """The hour-before for a 07:30 event is held to 07:00; for a 06:30 event it goes out at 05:30."""
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "schedule_event", title="School trip", starts_at="2026-10-08T07:30")
        await tool(conn, home, "schedule_event", title="Airport", starts_at="2026-10-09T06:30")
    sends = await run_worker(NOW, london("2026-10-09 08:00"), FakeAdapter())
    assert [(wall(at), text) for at, _, text in sends] == [
        ("Wed 7 Oct 07:30", "School trip tomorrow at 07:30"),
        ("Thu 8 Oct 07:00", "School trip today at 07:30"),        # due at 06:30, held until quiet hours end
        ("Fri 9 Oct 05:30", "Airport today at 06:30"),            # no day-before: that is in Thursday's brief
    ]


async def test_a_repeating_reminder_moves_to_its_next_time_and_keeps_local_time_over_the_clock_change():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "set_reminder", text="bins out", rrule="FREQ=WEEKLY;BYDAY=SU;BYHOUR=18",
                   target="household")
        await tool(conn, home, "set_reminder", text="antibiotics", rrule="FREQ=DAILY;UNTIL=20261007",
                   fire_at="2026-10-06T08:00")

    async def state(text):
        (row,) = await rows("select status, fire_at from reminders where text = :t", t=text)
        return row["status"], wall(row["fire_at"]), row["fire_at"].hour

    assert await jobs.fire_reminders(london("2026-10-06 08:00")) == 1
    assert await state("antibiotics") == ("scheduled", "Wed 7 Oct 08:00", 7)
    assert await jobs.fire_reminders(london("2026-10-07 08:00")) == 1
    assert (await state("antibiotics"))[0] == "sent"                        # its end date has passed

    assert await jobs.fire_reminders(london("2026-10-11 18:00")) == 1
    assert await state("bins out") == ("scheduled", "Sun 18 Oct 18:00", 17)
    assert await jobs.fire_reminders(london("2026-10-11 18:00")) == 0
    assert await jobs.fire_reminders(london("2026-10-18 18:00")) == 1
    assert await state("bins out") == ("scheduled", "Sun 25 Oct 18:00", 18)  # 18:00 GMT, an hour later in UTC

    # The worker was down for three weeks: one send, then the next future Sunday, not three sends.
    assert await jobs.fire_reminders(london("2026-11-12 09:00")) == 1
    assert await state("bins out") == ("scheduled", "Sun 15 Nov 18:00", 18)
    assert len(await rows("select 1 from outbox where text = 'bins out'")) == 3


async def test_after_an_outage_stale_event_reminders_are_dropped_not_sent():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "schedule_event", title="GP", starts_at="2026-10-07T10:30")
        await tool(conn, home, "schedule_event", title="Dentist", starts_at="2026-10-07T15:00")
        await tool(conn, home, "schedule_event", title="Chatterbox", starts_at="2026-10-06T09:00",
                   rrule="FREQ=WEEKLY", remind_before_minutes=[60])
    # Back up at 14:30 on Wednesday: the GP is over, the dentist is in half an hour, and
    # Chatterbox (Tuesday 09:00) is long gone.
    assert await jobs.fire_reminders(london("2026-10-07 14:30")) == 1
    assert [r["text"] for r in await rows("select text from outbox")] == ["Dentist today at 15:00"]
    statuses = await rows("select text, status from reminders order by fire_at")
    assert [(r["text"], r["status"]) for r in statuses] == [
        ("Chatterbox today at 09:00", "cancelled"),
        ("GP tomorrow at 10:30", "cancelled"), ("Dentist tomorrow at 15:00", "cancelled"),
        ("GP today at 10:30", "cancelled"), ("Dentist today at 15:00", "sent"),
    ]


async def test_a_reminder_a_few_minutes_late_is_still_sent():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "schedule_event", title="Standup", starts_at="2026-10-06T10:00",
                   remind_before_minutes=[0])
    assert await jobs.fire_reminders(london("2026-10-06 10:05")) == 1


# ---------------------------------------------------------------- recurrence
async def test_the_recurrence_job_adds_48_hours_of_reminders_once_and_skips_exception_dates():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "schedule_event", title="Chatterbox", starts_at="2026-10-06T09:00",
                   rrule="FREQ=WEEKLY;BYDAY=TU,TH", participants=["Tobi"])
        await tool(conn, home, "schedule_event", title="Old club", starts_at="2026-10-06T09:00", rrule="FREQ=DAILY")
        await tool(conn, home, "modify_event", event="Old club", scope="all", cancel=True)
        await tool(conn, home, "modify_event", event="Chatterbox", occurrence="2026-10-13", cancel=True)

    async def fire_times():
        return [wall(r["fire_at"]) for r in await rows(
            "select fire_at from reminders where status = 'scheduled' order by fire_at")]

    assert await fire_times() == ["Tue 6 Oct 08:00"]                       # made when it was scheduled
    assert await jobs.expand_recurrence(london("2026-10-06 13:00")) == 2   # Thursday the 8th is now in range
    assert await jobs.expand_recurrence(london("2026-10-06 13:00")) == 0
    assert await fire_times() == ["Tue 6 Oct 08:00", "Wed 7 Oct 09:00", "Thu 8 Oct 08:00"]

    # A week on: Tuesday the 13th was skipped, so only Thursday the 15th gets rows.
    assert await jobs.expand_recurrence(london("2026-10-13 10:00")) == 2
    assert (await fire_times())[-2:] == ["Wed 14 Oct 09:00", "Thu 15 Oct 08:00"]
    assert await rows("select 1 from reminders r join events e on e.id = r.event_id "
                      "where e.title = 'Old club' and r.status = 'scheduled'") == []


# ---------------------------------------------------------------- daily brief
async def expiring(conn, home, name, on):
    await add_item(conn, home, name, location="freezer", qty=1)
    await execute(conn, "update stock set expires_on = :on", on=on)


async def test_the_daily_brief_goes_out_once_at_digest_time_with_what_is_due_today():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "schedule_event", title="GP", starts_at="2026-10-07T10:30", participants=["Ada"])
        await tool(conn, home, "schedule_event", title="Yesterday", starts_at="2026-10-06T10:00")
        await tool(conn, home, "schedule_event", title="Friday thing", starts_at="2026-10-09T10:00")
        await tool(conn, home, "schedule_event", title="Airport", starts_at="2026-10-08T06:30")
        await tool(conn, home, "set_reminder", text="call the landlord", fire_at="2026-10-07T12:00")
        await expiring(conn, home, "chicken thighs", date(2026, 10, 8))

    assert await jobs.daily_brief(london("2026-10-07 07:29")) == 0
    assert await rows("select 1 from job_runs") == []
    assert await jobs.daily_brief(london("2026-10-07 07:30")) == 1
    assert await jobs.daily_brief(london("2026-10-07 07:31")) == 0
    assert await jobs.daily_brief(london("2026-10-07 09:00")) == 0

    (brief,) = await rows("select text, target, dedupe_key, urgency from outbox")
    assert (brief["target"], brief["dedupe_key"]) == ("household", "daily_brief:2026-10-07")
    for due in ("10:30 GP (Ada)", "12:00 call the landlord", "chicken thighs (freezer, 8 Oct)"):
        assert due in brief["text"]
    # Thursday's 06:30 is in the brief because its day-before reminder fell in quiet hours.
    assert "06:30 Airport" in brief["text"]
    assert "Yesterday" not in brief["text"] and "Friday thing" not in brief["text"]
    assert [r["run_key"] for r in await rows("select run_key from job_runs where job = 'daily_brief'")] == [
        "2026-10-07"]

    adapter = FakeAdapter()
    await send(london("2026-10-07 07:30"), adapter)
    assert sorted(chat for chat, _, _ in adapter.sent) == ["1001", "1002"]

    assert await jobs.daily_brief(london("2026-10-08 07:30")) == 1          # and again the next morning
    assert len(await rows("select 1 from outbox where target = 'household'")) == 2


async def test_no_brief_on_an_empty_day_and_none_sent_late():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "schedule_event", title="GP", starts_at="2026-10-08T10:30")
    assert await jobs.daily_brief(london("2026-10-07 07:30")) == 0           # nothing due on the 7th
    assert await rows("select 1 from outbox") == []
    assert len(await rows("select 1 from job_runs")) == 1                    # but the day is claimed

    # The worker was down until the afternoon of the 8th: too late to be a morning brief.
    assert await jobs.daily_brief(london("2026-10-08 13:00")) == 0
    assert await rows("select 1 from outbox") == []


async def test_two_workers_send_one_brief_and_digest_time_is_household_local_after_the_clock_change():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "schedule_event", title="GP", starts_at="2026-10-26T10:30")
        await execute(conn, "update households set digest_time = '08:15'")
    # Monday 26 October is GMT: 08:15 local is 08:15 UTC, not 07:15 UTC as it was the week before.
    assert await jobs.daily_brief(london("2026-10-26 08:15") - timedelta(hours=1)) == 0
    sent = await asyncio.gather(*(jobs.daily_brief(london("2026-10-26 08:15")) for _ in range(4)))
    assert sum(sent) == 1 and len(await rows("select 1 from outbox")) == 1


# ---------------------------------------------------------------- weekly digest
async def test_the_weekly_digest_goes_out_once_on_sunday_evening():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "schedule_event", title="Chatterbox", starts_at="2026-10-06T09:00",
                   rrule="FREQ=WEEKLY;BYDAY=TU", participants=["Tobi"])
        await tool(conn, home, "schedule_event", title="GP", starts_at="2026-10-14T10:30")
        await tool(conn, home, "schedule_event", title="Next month", starts_at="2026-11-14T10:30")
        await tool(conn, home, "update_shopping_list", add=[{"item": "eggs"}, {"item": "bread"}])
        await add_item(conn, home, "rice", status="low")

    sunday = london("2026-10-11 18:00")
    for not_yet in (london("2026-10-10 18:00"), sunday - timedelta(minutes=1), london("2026-10-12 18:00")):
        assert await jobs.weekly_digest(not_yet) == 0
    assert await jobs.weekly_digest(sunday) == 1
    assert await jobs.weekly_digest(sunday + timedelta(minutes=5)) == 0

    (digest,) = await rows("select text, target, dedupe_key from outbox")
    assert (digest["target"], digest["dedupe_key"]) == ("household", "weekly_digest:2026-W41")
    for expected in ("Tue 13 Oct 09:00 Chatterbox (Tobi)", "Wed 14 Oct 10:30 GP", "2 items", "rice"):
        assert expected in digest["text"]
    assert "Next month" not in digest["text"]
    assert await jobs.weekly_digest(london("2026-10-18 18:00")) == 1         # next Sunday is a new week
    assert [r["run_key"] for r in await rows("select run_key from job_runs order by run_key")] == [
        "2026-W41", "2026-W42"]


async def test_no_weekly_digest_when_there_is_nothing_to_say():
    async with tx() as conn:
        await family(conn)
    assert await jobs.weekly_digest(london("2026-10-11 18:00")) == 0
    assert await rows("select 1 from outbox") == []


# ---------------------------------------------------------------- the worker loop
async def test_the_inbound_job_wakes_on_notify_and_closes_its_listener_when_cancelled(client):
    listeners = "select 1 from pg_stat_activity where datname = current_database() and query ilike 'listen%'"
    async with tx() as conn:
        await seed_home(conn)
    job = asyncio.create_task(jobs.inbound_job(get_settings(), LoopRuntime(FakeLLM(say("NOOP"))), {}, None,
                                               asyncio.Event()))
    await asyncio.sleep(0.3)                       # it has found nothing and is waiting out its 2 s poll
    assert len(await rows(listeners)) == 1

    posted = time.monotonic()
    await client.post("/webhooks/telegram", json=tg_update(1, "morning all"),
                      headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"})
    while (await rows("select status from messages"))[0]["status"] != "processed":
        assert time.monotonic() - posted < 1.0, "the job waited for its poll instead of waking on NOTIFY"
        await asyncio.sleep(0.02)

    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    for _ in range(50):
        if not await rows(listeners):
            break
        await asyncio.sleep(0.02)
    assert await rows(listeners) == []
