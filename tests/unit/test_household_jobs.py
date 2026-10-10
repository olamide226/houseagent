"""Household jobs on a controlled clock: the bins, the MOT, a prescription, a birthday (ADR 0037).

What is said and when, who it is said to, what "done", "later", a tick, "skip" and "stop" leave in
the row, and that a plain reminder is none the wiser. Every case starts on Monday 5 October 2026
at 12:00 in London unless it says otherwise.
"""
import json
from datetime import timedelta

import pytest
import respx

from app.agent.base import ToolError
from app.agent.loop import LoopRuntime
from app.agent.prompt import build_brief
from app.agent.tools import run_tool
from app.channels.base import ADAPTERS
from app.core.envelope import Channel
from app.core.timeutil import local, utcnow
from app.db import execute, fetch_all, tx
from app.pipeline import inbound, router
from app.services import calendar, households
from app.services import jobs as household_jobs
from app.worker import jobs
from tests.helpers import FakeAdapter, FakeLLM, add_member, ctx_for, london, say, seed_home, technical_words, wall
from tests.unit.test_dashboard import login
from tests.unit.test_loop import envelope

NOW = london("2026-10-05 12:00")     # a Monday
TUESDAY_7PM = london("2026-10-06 19:00")
BINS = {"title": "Put the bins out", "due_on": "2026-10-06", "at_time": "19:00", "rrule": "FREQ=WEEKLY;BYDAY=TU",
        "owner": "Ola"}
REPLY = "Reply done when it's sorted, or later to be asked again."
SECRET = {"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"}
TG = "https://api.telegram.org/bot424242:TEST-TOKEN"
JOB = """select r.id, r.text, r.kind, r.status, r.target, r.fire_at, r.due_at, r.all_day, r.lead_days, r.rrule,
                r.urgency, r.asked_at, r.done_at, m.name as owner, d.name as done_by
         from reminders r left join members m on m.id = r.member_id left join members d on d.id = r.done_by
         where r.kind is not null order by r.due_at, r.text"""


async def family(conn):
    home = await seed_home(conn)                          # Ola, Telegram 1001
    await add_member(conn, home, "Ada", telegram_id="1002")
    await add_member(conn, home, "Tobi", role="child")
    return home


async def tool(conn, home, name, *, member="Ola", now=NOW, fails=False, **args):
    result, is_error = await run_tool(name, args, ctx_for(conn, home, member, now=now))
    assert is_error == fails, result
    return result


async def rows(sql=JOB, **params):
    async with tx() as conn:
        return await fetch_all(conn, sql, **params)


async def said(start, end, adapter, step=timedelta(minutes=30)):
    """What the worker sends between two moments: (London time, chat, text) for every send."""
    sends, now = [], start
    while now <= end:
        await jobs.nudge_household_jobs(now)
        already = len(adapter.sent)
        while await router.dispatch_due({Channel.telegram: adapter}, now=now):
            pass
        sends += [(wall(now), chat, text) for chat, text, _ in adapter.sent[already:]]
        now += step
    return sends


# ---------------------------------------------------------------- setting one up
async def test_the_bins_every_tuesday_night_are_olas_and_are_one_reminder_row():
    async with tx() as conn:
        home = await family(conn)
        result = await tool(conn, home, "add_job", member="Ada", **BINS)
        again = await tool(conn, home, "add_job", member="Ada", **BINS)

    assert result == ("OK: Put the bins out for Ola, due Tue 6 Oct at 19:00, repeating (FREQ=WEEKLY;BYDAY=TU)\n"
                      "NOTE: first nudge on Tue 6 Oct at 19:00; it is ticked off when someone says done")
    assert again == "OK: Put the bins out for Ola, due Tue 6 Oct at 19:00, repeating (FREQ=WEEKLY;BYDAY=TU) is already set"
    (job,) = await rows()
    assert (job["text"], job["kind"], job["owner"], job["status"]) == ("Put the bins out", "job", "Ola", "scheduled")
    assert (job["due_at"], job["fire_at"], job["rrule"]) == (TUESDAY_7PM, TUESDAY_7PM, "FREQ=WEEKLY;BYDAY=TU")
    assert (job["all_day"], job["lead_days"], job["asked_at"], job["done_at"]) == (False, 0, None, None)
    (action,) = await rows("select tool, member_id, inverse from agent_actions")     # said once, logged once, undoable
    assert (action["tool"], action["member_id"]) == ("add_job", home.members["Ada"])
    assert action["inverse"] == [{"op": "delete_rows", "table": "reminders", "ids": [job["id"]]}]


async def test_a_day_with_no_time_is_due_all_that_day_and_a_rare_one_is_said_two_weeks_ahead():
    async with tx() as conn:
        home = await family(conn)
        mot = await tool(conn, home, "add_job", title="MOT", due_on="2027-03-14")
        pills = await tool(conn, home, "add_job", title="Ada's prescription", due_on="2026-10-05",
                           rrule="FREQ=DAILY;INTERVAL=28", owner="Ada")
        fish = await tool(conn, home, "add_job", title="Feed the fish", due_on="2026-10-05", at_time="17:00",
                          rrule="FREQ=DAILY", owner="Tobi")
        ours = await tool(conn, home, "add_job", title="Car insurance", due_on="2026-11-03", owner="everyone",
                          remind_days_before=28)

    assert mot == ("OK: MOT for Ola, due Sun 14 Mar 2027\n"
                   "NOTE: first nudge on Sun 28 Feb at 09:00; it is ticked off when someone says done")
    # Due today, said at noon: the morning's nudge has passed, so the first one is tomorrow's, if it is not done.
    assert pills == ("OK: Ada's prescription for Ada, due Mon 5 Oct, repeating (FREQ=DAILY;INTERVAL=28)\n"
                     "NOTE: first nudge on Tue 6 Oct at 09:00; it is ticked off when someone says done")
    assert fish.startswith("OK: Feed the fish for Tobi, due Mon 5 Oct at 17:00, repeating (FREQ=DAILY)")
    assert ours.startswith("OK: Car insurance for everyone, due Tue 3 Nov\nNOTE: first nudge on Tue 6 Oct at 09:00")
    found = {job["text"]: job for job in await rows()}
    assert [(name, job["all_day"], job["lead_days"], wall(job["due_at"]), job["owner"], job["target"])
            for name, job in found.items()] == [
        ("Ada's prescription", True, 0, "Mon 5 Oct 09:00", "Ada", "member"),
        ("Feed the fish", False, 0, "Mon 5 Oct 17:00", "Tobi", "member"),
        ("Car insurance", True, 28, "Tue 3 Nov 09:00", None, "household"),
        ("MOT", True, 14, "Sun 14 Mar 09:00", "Ola", "member"),
    ]


@pytest.mark.parametrize("args,problem", [
    ({"title": "MOT", "due_on": "2026-10-04"}, "Sun 4 Oct has already passed; it is now Mon 5 Oct 2026 12:00"),
    ({"title": "Bins", "due_on": "2026-10-05", "at_time": "08:00"}, "Mon 5 Oct at 08:00 has already passed"),
    ({"title": "Bins", "due_on": "2026-10-06", "rrule": "FREQ=WEEKLY;COUNT=3"}, "an end date (UNTIL) instead of COUNT"),
    ({"title": "Bins", "due_on": "2026-10-06", "rrule": "EVERY TUESDAY"}, "not valid RFC 5545"),
    ({"title": " ", "due_on": "2026-10-06"}, "say what the job is"),
    ({"title": "Bins", "due_on": "2026-10-06", "owner": "Grandpa"}, "nobody called 'Grandpa' is in the family"),
])
async def test_a_job_that_cannot_be_set_says_why_and_writes_nothing(args, problem):
    async with tx() as conn:
        home = await family(conn)
        assert problem in await tool(conn, home, "add_job", fails=True, **args)
    assert await rows() == [] and await rows("select 1 from agent_actions") == []


# ---------------------------------------------------------------- what is said, and when
async def test_the_bins_are_said_on_the_night_asked_about_once_the_morning_after_and_said_again_next_week():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "add_job", member="Ada", **BINS)
    adapter = FakeAdapter()

    assert await said(NOW, london("2026-10-13 19:00"), adapter) == [
        ("Tue 6 Oct 19:00", "1001", f"To do today at 19:00: Put the bins out. {REPLY}"),
        ("Wed 7 Oct 09:00", "1001", "Still to do: Put the bins out, due yesterday at 19:00. Reply done if it's "
                                    "sorted. If not, I'll leave it until the next one."),
        ("Tue 13 Oct 19:00", "1001", f"To do today at 19:00: Put the bins out. {REPLY}"),
    ]
    (job,) = await rows()         # never ticked off: it is simply waiting on this week's now
    assert (wall(job["due_at"]), wall(job["asked_at"]), wall(job["fire_at"]), job["status"], job["done_at"]) == (
        "Tue 13 Oct 19:00", "Tue 13 Oct 19:00", "Wed 14 Oct 09:00", "scheduled", None)
    assert home.ola and technical_words(" ".join(text for _, text, _ in adapter.sent)) == []


async def test_the_mot_is_said_two_weeks_ahead_on_the_day_and_the_morning_after_and_then_never_again():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "add_job", title="MOT", due_on="2027-03-14")
    adapter = FakeAdapter()

    assert await said(london("2027-02-27 12:00"), london("2027-03-20 12:00"), adapter, timedelta(hours=1)) == [
        ("Sun 28 Feb 09:00", "1001", f"Coming up: MOT, due on Sun 14 Mar (in 2 weeks). {REPLY}"),
        ("Sun 14 Mar 09:00", "1001", f"Still to do: MOT, due today. {REPLY}"),
        ("Mon 15 Mar 09:00", "1001", "Still to do: MOT, due yesterday. Reply done once it's sorted. I won't keep asking."),
    ]
    (job,) = await rows()         # everything has been said; it waits to be ticked off, and says no more
    assert (job["status"], wall(job["due_at"]), job["done_at"]) == ("sent", "Sun 14 Mar 09:00", None)
    assert home.ola and await jobs.nudge_household_jobs(london("2027-06-01 09:00")) == 0


async def test_a_birthday_is_said_a_week_ahead_and_on_the_day_and_comes_round_next_year_with_nothing_to_tick_off():
    async with tx() as conn:
        home = await family(conn)
        result = await tool(conn, home, "add_job", title="Grandma's birthday", due_on="2026-11-12", kind="date",
                            owner="everyone")                      # nobody has to say that it is every year
    assert result == ("OK: Grandma's birthday for everyone, on Thu 12 Nov, repeating (FREQ=YEARLY)\n"
                      "NOTE: nothing to tick off; it is said on Thu 5 Nov at 09:00 and on the day")
    adapter = FakeAdapter()

    sends = await said(london("2026-11-04 12:00"), london("2026-11-14 12:00"), adapter, timedelta(hours=1))
    assert sends == [(at, chat, text) for at, text in [                   # no family group: each adult is told
        ("Thu 5 Nov 09:00", "Coming up: Grandma's birthday, on Thu 12 Nov (in a week)."),
        ("Thu 12 Nov 09:00", "Today: Grandma's birthday."),
    ] for chat in ("1002", "1001")]                                       # Ada, then Ola. Made together: by name
    (job,) = await rows()
    assert (wall(job["due_at"]), job["due_at"].year, wall(job["fire_at"]), job["status"], job["asked_at"]) == (
        "Fri 12 Nov 09:00", 2027, "Fri 5 Nov 09:00", "scheduled", None)
    async with tx() as conn:
        assert "is a date, not a job: there is nothing to tick off" in await tool(
            conn, home, "update_job", job="grandma", action="done", fails=True)


async def test_a_daily_job_is_not_asked_about_again_once_the_next_day_has_its_own_nudge():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "add_job", title="Feed the fish", due_on="2026-10-05", at_time="08:00",
                   rrule="FREQ=DAILY", owner="Tobi")           # said at noon: the first one is tomorrow's
    adapter = FakeAdapter()

    sends = await said(NOW, london("2026-10-07 12:00"), adapter)
    # A child has no chat: every adult is told (Ada, then Ola: made together, by name), and told whose
    # job it is. 08:00 comes before the morning after's 09:00, so there is one message a day and never a second.
    assert sends == [(at, chat, "To do today at 08:00: Feed the fish (Tobi). Reply done if it's sorted. "
                                "If not, I'll leave it until the next one.")
                     for at in ("Tue 6 Oct 08:00", "Wed 7 Oct 08:00") for chat in ("1002", "1001")] and home.ola


async def test_a_time_somebody_picked_is_said_then_even_in_quiet_hours_and_a_morning_nudge_waits_for_them_to_end():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "add_job", title="Lock the back gate", due_on="2026-10-05", at_time="22:00",
                   rrule="FREQ=DAILY")
        await tool(conn, home, "add_job", title="Ada's prescription", due_on="2026-10-06",
                   rrule="FREQ=DAILY;INTERVAL=28", owner="Ada")
        await execute(conn, "update members set quiet_start = '21:30', quiet_end = '10:15' where id = :id",
                      id=home.members["Ada"])
    assert [(job["text"], job["urgency"]) for job in await rows()] == [
        ("Lock the back gate", "high"), ("Ada's prescription", "normal")]
    adapter = FakeAdapter()

    sends = await said(NOW, london("2026-10-06 12:00"), adapter)
    assert [(at, chat, text.split(":")[0]) for at, chat, text in sends] == [
        ("Mon 5 Oct 22:00", "1001", "To do today at 22"),             # Ola's quiet hours began at 21:30
        ("Tue 6 Oct 09:00", "1001", "Still to do"),                   # the morning after: not urgent, and not held
        ("Tue 6 Oct 10:30", "1002", "To do today"),                   # due at 09:00, held until Ada's end at 10:15
    ]
    assert [row["urgency"] for row in await rows("select urgency from outbox order by created_at")] == [
        "high", "normal", "normal"]


async def test_after_the_worker_was_away_a_job_is_said_once_for_the_occurrence_that_is_current_now():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "add_job", **BINS)
    adapter = FakeAdapter()

    back = london("2026-10-21 10:00")                                   # down for more than two weeks
    assert await jobs.nudge_household_jobs(back) == 1 and await jobs.nudge_household_jobs(back) == 0
    await router.dispatch_due({Channel.telegram: adapter}, now=back)
    assert [text for _, text, _ in adapter.sent] == [
        "Still to do: Put the bins out, due yesterday at 19:00. Reply done if it's sorted. If not, I'll leave it "
        "until the next one."]
    (job,) = await rows()
    assert (wall(job["due_at"]), wall(job["fire_at"])) == ("Tue 20 Oct 19:00", "Tue 27 Oct 19:00") and home.ola


async def test_two_runs_of_the_worker_at_the_same_moment_send_one_nudge():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "add_job", **BINS)
    assert await jobs.nudge_household_jobs(TUESDAY_7PM - timedelta(seconds=1)) == 0
    assert await jobs.nudge_household_jobs(TUESDAY_7PM) == 1 and await jobs.nudge_household_jobs(TUESDAY_7PM) == 0
    (queued,) = await rows("select target, member_id, urgency, dedupe_key from outbox")
    (job,) = await rows()
    assert (queued["target"], queued["member_id"], queued["urgency"]) == ("member", home.ola, "normal")
    assert household_jobs.named_in(queued["dedupe_key"]) == {"job": job["id"], "due": int(TUESDAY_7PM.timestamp())}
    assert household_jobs.named_in(queued["dedupe_key"] + ":" + home.ola)["job"] == job["id"]   # sent per adult
    assert household_jobs.named_in("reminder:x:y") == {} and household_jobs.named_in(None) == {}


# ---------------------------------------------------------------- done, later, skip, change, stop
async def test_done_ticks_this_weeks_off_sets_next_weeks_and_nothing_more_is_asked():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "add_job", member="Ada", **BINS)
    adapter = FakeAdapter()
    await said(NOW, TUESDAY_7PM, adapter)
    at = TUESDAY_7PM + timedelta(minutes=30)
    async with tx() as conn:
        result = await tool(conn, home, "update_job", now=at, action="done")       # the one that is waiting
        twice = await tool(conn, home, "update_job", now=at + timedelta(minutes=1), job="the bins", action="done")

    assert result == "OK: Put the bins out done. Next: Tue 13 Oct at 19:00"
    # Said again a minute later, it is not next week's that is ticked off.
    assert twice == "OK: nothing to tick off: Put the bins out was ticked off on Tue 6 Oct at 19:30, and is next due Tue 13 Oct at 19:00"
    (job,) = await rows()
    assert (wall(job["due_at"]), wall(job["fire_at"]), job["asked_at"], job["status"]) == (
        "Tue 13 Oct 19:00", "Tue 13 Oct 19:00", None, "scheduled")
    assert (job["done_at"], job["done_by"]) == (at, "Ola")
    assert [row["tool"] for row in await rows("select tool from agent_actions order by created_at")] == [
        "add_job", "update_job"]
    assert await said(at, london("2026-10-13 18:30"), adapter) == []                # no morning-after question


async def test_done_before_the_nudge_counts_when_the_job_is_nearly_due_and_a_one_off_is_ticked_off_for_good():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "add_job", **BINS)
        await tool(conn, home, "add_job", title="MOT", due_on="2027-03-14")
        early = await tool(conn, home, "update_job", member="Ada", now=london("2026-10-06 17:00"), job="bins",
                           action="done")
        mot = await tool(conn, home, "update_job", now=london("2026-12-01 10:00"), job="MOT", action="done")
        again = await tool(conn, home, "update_job", now=london("2026-12-01 10:01"), job="MOT", action="done", fails=True)

    assert early == "OK: Put the bins out done. Next: Tue 13 Oct at 19:00" and mot == "OK: MOT done"
    assert "no job is called anything like 'MOT'" in again                 # it is no longer one of the jobs
    bins, done = await rows()
    assert (wall(bins["due_at"]), bins["done_by"]) == ("Tue 13 Oct 19:00", "Ada")
    assert (done["status"], done["done_by"], done["done_at"]) == ("acked", "Ola", london("2026-12-01 10:00"))
    sends = await said(london("2027-02-27 12:00"), london("2027-03-16 12:00"), FakeAdapter(), timedelta(hours=3))
    assert sends and not [text for _, _, text in sends if "MOT" in text]      # the bins go on; the MOT is over


async def test_later_asks_again_in_two_hours_for_a_job_with_a_time_and_the_next_morning_for_a_day():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "add_job", **BINS)
        await tool(conn, home, "add_job", title="MOT", due_on="2026-10-30", remind_days_before=24)   # said tomorrow
    adapter = FakeAdapter()
    await said(NOW, TUESDAY_7PM, adapter)
    at = TUESDAY_7PM + timedelta(minutes=5)
    async with tx() as conn:
        bins = await tool(conn, home, "update_job", now=at, job="bins", action="later")
        mot = await tool(conn, home, "update_job", now=at, job="mot", action="later")

    assert bins == "OK: will ask again about Put the bins out on Tue 6 Oct at 21:05"
    assert mot == "OK: will ask again about MOT on Wed 7 Oct at 09:00"
    assert await said(at, london("2026-10-07 12:05"), adapter) == [
        ("Tue 6 Oct 21:05", "1001", f"Still to do: Put the bins out, due today at 19:00. {REPLY}"),
        ("Wed 7 Oct 09:05", "1001", f"Coming up: MOT, due on Fri 30 Oct (in 23 days). {REPLY}"),
        ("Wed 7 Oct 09:05", "1001", "Still to do: Put the bins out, due yesterday at 19:00. Reply done if it's "
                                    "sorted. If not, I'll leave it until the next one."),
    ]


async def test_later_with_a_time_is_said_at_that_time_even_in_quiet_hours_and_a_time_gone_by_is_refused():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "add_job", **BINS)
    adapter = FakeAdapter()
    await said(NOW, TUESDAY_7PM, adapter)
    async with tx() as conn:
        past = await tool(conn, home, "update_job", now=TUESDAY_7PM, action="later", later_until="2026-10-06T18:00",
                          fails=True)
        await tool(conn, home, "update_job", now=TUESDAY_7PM, action="later", later_until="2026-10-06T23:00")
    assert "Tue 6 Oct at 18:00 has already passed" in past
    (job,) = await rows()
    assert (wall(job["fire_at"]), job["urgency"], wall(job["due_at"])) == ("Tue 6 Oct 23:00", "high", "Tue 6 Oct 19:00")
    sends = await said(TUESDAY_7PM, london("2026-10-07 08:00"), adapter)
    assert [at for at, _, _ in sends] == ["Tue 6 Oct 23:00"] and home.ola
    async with tx() as conn:          # never put off past next week's own nudge
        far = await tool(conn, home, "update_job", now=london("2026-10-07 08:00"), job="bins", action="later",
                         later_until="2026-10-20T10:00")
    assert far == "OK: will ask again about Put the bins out on Tue 13 Oct at 19:00"


async def test_skip_leaves_this_one_out_change_moves_it_or_gives_it_to_someone_else_and_stop_ends_it():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "add_job", **BINS)
        await tool(conn, home, "add_job", title="MOT", due_on="2027-03-14")
        skipped = await tool(conn, home, "update_job", job="bins", action="skip")
        no_repeat = await tool(conn, home, "update_job", job="MOT", action="skip", fails=True)
        hers = await tool(conn, home, "update_job", job="bins", action="change", owner="Ada")
        earlier = await tool(conn, home, "update_job", job="bins", action="change", at_time="18:30")
        moved = await tool(conn, home, "update_job", job="MOT", action="change", due_on="2027-03-20", owner="everyone")
        nothing = await tool(conn, home, "update_job", job="MOT", action="change", fails=True)
        stopped = await tool(conn, home, "update_job", job="MOT", action="stop")

    assert skipped == "OK: Put the bins out skipped for Tue 6 Oct at 19:00. Next: Tue 13 Oct at 19:00"
    assert "MOT does not come round again: stop it instead" in no_repeat
    assert hers == "OK: now Put the bins out for Ada, due Tue 13 Oct at 19:00, repeating (FREQ=WEEKLY;BYDAY=TU)"
    assert earlier == "OK: now Put the bins out for Ada, due Tue 13 Oct at 18:30, repeating (FREQ=WEEKLY;BYDAY=TU)"
    assert moved == "OK: now MOT for everyone, due Sat 20 Mar 2027" and "say what to change" in nothing
    assert stopped == "OK: MOT stopped"
    bins, mot = await rows()
    assert (bins["owner"], bins["target"], wall(bins["due_at"]), wall(bins["fire_at"])) == (
        "Ada", "member", "Tue 13 Oct 18:30", "Tue 13 Oct 18:30")
    assert (mot["status"], mot["owner"], mot["target"], wall(mot["due_at"]), mot["lead_days"]) == (
        "cancelled", None, "household", "Sat 20 Mar 09:00", 14)
    adapter = FakeAdapter()
    sends = await said(NOW, london("2026-10-13 19:00"), adapter)             # skipped this week; Ada's from the next
    assert [(at, chat) for at, chat, _ in sends] == [("Tue 13 Oct 18:30", "1002")] and home.ola
    later = await said(london("2027-03-05 12:00"), london("2027-03-22 12:00"), adapter, timedelta(hours=3))
    assert later and not [text for _, _, text in later if "MOT" in text]      # stopped means nothing more is said


async def test_which_job_is_meant_is_the_named_one_the_one_waiting_or_a_question():
    async with tx() as conn:
        home = await family(conn)
        for owner in ("Ola", "Ada"):
            await tool(conn, home, "add_job", title=f"{owner}'s prescription", due_on="2026-10-06",
                       rrule="FREQ=DAILY;INTERVAL=28", owner=owner)
        await tool(conn, home, "add_job", **BINS)
        none_waiting = await tool(conn, home, "update_job", action="done", fails=True)
        either = await tool(conn, home, "update_job", job="prescription", action="done", fails=True)
        unknown = await tool(conn, home, "update_job", job="the car", action="done", fails=True)
    assert none_waiting == ("ERROR: say which job. Waiting to be ticked off: none. All the jobs: Ada's prescription, "
                            "Ola's prescription, Put the bins out")
    assert either == ("ERROR: 'prescription' could be Ada's prescription, Ola's prescription. Ask which, in one "
                      "short question")
    assert unknown.startswith("ERROR: no job is called anything like 'the car'. The jobs: Ada's prescription")

    await said(NOW, london("2026-10-06 09:00"), FakeAdapter())            # both prescriptions have been said
    async with tx() as conn:
        two_waiting = await tool(conn, home, "update_job", now=london("2026-10-06 09:30"), action="done", fails=True)
        hers = await tool(conn, home, "update_job", member="Ada", now=london("2026-10-06 09:30"),
                          job="Ada's prescription", action="done")
        the_other = await tool(conn, home, "update_job", now=london("2026-10-06 09:31"), action="done")
    assert "Waiting to be ticked off: Ada's prescription, Ola's prescription" in two_waiting
    assert hers == "OK: Ada's prescription done. Next: Tue 3 Nov"
    assert the_other == "OK: Ola's prescription done. Next: Tue 3 Nov"     # the only one still waiting
    assert [(job["text"], job["done_by"]) for job in await rows() if job["done_by"]] == [
        ("Ada's prescription", "Ada"), ("Ola's prescription", "Ola")]


async def test_undo_puts_a_ticked_off_job_back_exactly_as_it_was_waiting():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "add_job", **BINS)
    await said(NOW, TUESDAY_7PM, FakeAdapter())
    before = await rows()
    async with tx() as conn:
        await tool(conn, home, "update_job", now=TUESDAY_7PM, action="done")
        undone = await tool(conn, home, "undo_last")
    assert undone == "OK: undid update_job (OK: Put the bins out done. Next: Tue 13 Oct at 19:00)"
    assert await rows() == before and before[0]["asked_at"] == TUESDAY_7PM
    async with tx() as conn:                                              # and setting it up can be undone too
        await tool(conn, home, "undo_last")
    assert await rows() == []


# ---------------------------------------------------------------- a tick on the nudge
def reaction(update_id, message_id, emoji="\U0001F44D", user_id=1001, chat_id=1001):
    return {"update_id": update_id, "message_reaction": {
        "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"}, "message_id": message_id,
        "user": {"id": user_id, "is_bot": False, "first_name": "Ola"}, "date": 1791230640, "old_reaction": [],
        "new_reaction": [{"type": "emoji", "emoji": emoji}]}}


async def react(client, home, update, at, *script):
    """A reaction arrives at `at` and the household's waiting messages are processed."""
    assert (await client.post("/webhooks/telegram", json=update, headers=SECRET)).status_code == 200
    async with tx() as conn:
        await execute(conn, "update messages set created_at = :at where status = 'received'", at=at)
    turns = await inbound.process_household(home.id, LoopRuntime(FakeLLM(*script)), ADAPTERS)
    while await router.dispatch_due(ADAPTERS):
        pass
    return turns


@respx.mock
async def test_a_thumbs_up_on_the_nudge_ticks_the_job_off_in_code_and_the_nudge_gets_one_back(client):
    send = respx.post(f"{TG}/sendMessage").respond(json={"ok": True, "result": {"message_id": 9001}})
    back = respx.post(f"{TG}/setMessageReaction").respond(json={"ok": True, "result": True})
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "add_job", member="Ada", **BINS)
    await jobs.nudge_household_jobs(TUESDAY_7PM)
    await router.dispatch_due(ADAPTERS, now=TUESDAY_7PM)
    (job,) = await rows()
    (nudge,) = await rows("select text, meta, external_id from messages where direction = 'out'")
    assert json.loads(send.calls.last.request.content)["chat_id"] == "1001" and nudge["external_id"] == "9001"
    assert nudge["meta"] == {"job": job["id"], "due": int(TUESDAY_7PM.timestamp())}

    at = TUESDAY_7PM + timedelta(minutes=10)
    assert await react(client, home, reaction(1, 9001, "\U0001F44D\U0001F3FD"), at) == 0   # no turn: FakeLLM has no script
    (job,) = await rows()
    assert (wall(job["due_at"]), job["done_by"], job["done_at"], job["asked_at"]) == ("Tue 13 Oct 19:00", "Ola", at, None)
    assert json.loads(back.calls.last.request.content) == {
        "chat_id": "1001", "message_id": 9001, "reaction": [{"type": "emoji", "emoji": "\U0001F44D"}]}
    (action,) = await rows("select tool, member_id, result from agent_actions where tool = 'update_job'")
    assert (action["member_id"], action["result"]) == (home.ola, "OK: Put the bins out done. Next: Tue 13 Oct at 19:00")

    # A second tick on the same nudge is about an occurrence that is ticked off: nothing moves, nothing is said.
    assert await react(client, home, reaction(2, 9001), at + timedelta(minutes=1)) == 0
    assert (await rows())[0]["due_at"] == job["due_at"] and back.call_count == 1 and send.call_count == 1
    assert await rows("select 1 from messages where status <> 'processed' and direction = 'in'") == []
    # Any other reaction on it, and a thumbs-up on any other message, is the agent's to read as before.
    assert await react(client, home, reaction(3, 9001, "\U0001F44E"), at + timedelta(minutes=2), say("NOOP")) == 1
    assert await react(client, home, reaction(4, 4242), at + timedelta(minutes=3), say("NOOP")) == 1
    assert (await rows())[0]["due_at"] == job["due_at"]


@respx.mock
async def test_in_the_family_group_whoever_ticks_the_nudge_did_the_job(client):
    group = -1001234567890
    respx.post(f"{TG}/sendMessage").respond(json={"ok": True, "result": {"message_id": 77}})
    respx.post(f"{TG}/setMessageReaction").respond(json={"ok": True, "result": True})
    async with tx() as conn:
        home = await family(conn)
        thread = await inbound._upsert_thread(conn, home.id, "telegram", str(group), "group")
        await execute(conn, "update households set primary_thread_id = :t where id = :h", t=thread, h=home.id)
        await tool(conn, home, "add_job", title="Feed the fish", due_on="2026-10-06", at_time="19:00",
                   rrule="FREQ=DAILY", owner="everyone")
    await jobs.nudge_household_jobs(TUESDAY_7PM)
    await router.dispatch_due(ADAPTERS, now=TUESDAY_7PM)
    (nudge,) = await rows("select text from messages where direction = 'out'")
    assert nudge["text"].startswith("To do today at 19:00: Feed the fish.")

    assert await react(client, home, reaction(1, 77, "✅", user_id=1002, chat_id=group), TUESDAY_7PM) == 0
    (job,) = await rows()
    assert (job["done_by"], wall(job["due_at"])) == ("Ada", "Wed 7 Oct 19:00")


# ---------------------------------------------------------------- beside reminders, in the brief and in what is coming up
async def test_a_job_is_not_a_reminder_to_anything_that_reads_or_sends_reminders():
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "add_job", **BINS)
        await tool(conn, home, "set_reminder", text="call the landlord", fire_at="2026-10-06T19:00")
        listed = await calendar.standalone_reminders(conn, home.id, NOW, NOW + timedelta(days=30))
        upcoming = await tool(conn, home, "list_upcoming", days=7)
        brief = await build_brief(conn, envelope(home, "hi"), NOW)
        export = await households.export(conn, home.id)
    assert [row["text"] for row in listed] == ["call the landlord"]
    assert upcoming == ("Tue 6 Oct 19:00 job: Put the bins out (Ola) [repeats]\n"
                        "Tue 6 Oct 19:00 reminder: call the landlord (Ola)")
    assert "Jobs: Put the bins out (Ola, due Tue 6 Oct at 19:00, repeats FREQ=WEEKLY;BYDAY=TU)" in brief.splitlines()
    assert sorted(row["kind"] or "" for row in export["reminders"]) == ["", "job"]

    assert await jobs.fire_reminders(TUESDAY_7PM) == 1                     # the reminder, and only the reminder
    assert [row["text"] for row in await rows("select text from outbox")] == ["call the landlord"]
    assert await jobs.nudge_household_jobs(TUESDAY_7PM) == 1
    async with tx() as conn:
        brief = await build_brief(conn, envelope(home, "done"), TUESDAY_7PM + timedelta(minutes=1))
        upcoming = await tool(conn, home, "list_upcoming", now=london("2026-10-07 08:00"), days=3)
    assert ("Jobs: Put the bins out (Ola, asked, not done yet, was due Tue 6 Oct at 19:00, "
            "repeats FREQ=WEEKLY;BYDAY=TU)") in brief.splitlines()
    assert upcoming == "Tue 6 Oct 19:00 job: Put the bins out (Ola) [asked, not done yet] [repeats]"


async def test_undoing_a_change_made_before_the_migration_still_works():
    """An undo copy of a reminder taken before the job columns existed has no value for them."""
    async with tx() as conn:
        home = await family(conn)
        await tool(conn, home, "set_reminder", text="call the landlord", fire_at="2026-10-06T19:00")
        (reminder,) = await fetch_all(conn, "select id from reminders")
        async with calendar_cancel(conn, home) as rec:
            await calendar.cancel_reminder(rec, reminder["id"])
        await execute(conn, """update agent_actions set inverse = jsonb_set(inverse, '{0,rows,0}',
                                 (inverse->0->'rows'->0) - 'kind' - 'due_at' - 'all_day' - 'lead_days' - 'asked_at'
                                 - 'done_at' - 'done_by') where tool = 'calendar.cancel_reminder'""")
        assert (await tool(conn, home, "undo_last")).startswith("OK: undid calendar.cancel_reminder")
        assert await fetch_all(conn, "select status, kind, all_day from reminders") == [
            {"status": "scheduled", "kind": None, "all_day": None}]


def calendar_cancel(conn, home):
    from app.agent.actions import record

    return record(ctx_for(conn, home, now=NOW), "calendar.cancel_reminder", {})


# ---------------------------------------------------------------- the Jobs page
async def a_job(home, title, days, *, at_time=None, rrule=None, owner=None, kind="job", asked=False):
    """A job due `days` from today, real time, as the pages read the real clock."""
    from datetime import time as clock

    from app.agent.actions import Recorder

    async with tx() as conn:
        today = local(utcnow(), "Europe/London").date()
        job_id = await household_jobs.add(
            Recorder(ctx_for(conn, home)), title=title, due_on=today + timedelta(days=days), rrule=rrule, kind=kind,
            at_time=clock.fromisoformat(at_time) if at_time else None, member_id=owner and home.members[owner])
        if asked:
            await execute(conn, "update reminders set asked_at = now() where id = :id", id=job_id)
    return job_id


def clock(moment):
    """London wall-clock time, so a week later is a week later across a clock change."""
    return local(moment, "Europe/London").replace(tzinfo=None)


async def test_the_jobs_page_lists_what_is_coming_up_and_whose_it_is_in_plain_words(client):
    async with tx() as conn:
        home = await family(conn)
    bins = await a_job(home, "Put the bins out", 1, at_time="19:00", rrule="FREQ=WEEKLY", owner="Ola")
    await a_job(home, "Ada's prescription", 0, rrule="FREQ=DAILY;INTERVAL=28", owner="Ada", asked=True)
    await a_job(home, "Grandma's birthday", 40, rrule="FREQ=YEARLY", kind="date")
    await login(client, home)

    page = (await client.get("/dashboard/jobs")).text
    assert "<title>Jobs</title>" in page and 'href="/dashboard/jobs" aria-current="page"' in page
    waiting, coming = page.split("<h2>Coming up</h2>")
    assert "<h2>Not done yet</h2>" in waiting and "Ada&#39;s prescription" in waiting
    assert "Ada · due today · every 28 days" in waiting
    assert "Put the bins out" in coming and "Ola · due tomorrow at 19:00 · every week" in coming
    assert "Grandma&#39;s birthday" in coming and "Everyone · on " in coming and "every year" in coming
    assert f'hx-post="/dashboard/jobs/{bins}/done"' in coming and page.count('class="tick"') == 2   # no circle on a date
    assert "tell the assistant" in coming and technical_words(page.split('<main id="main">')[1].split("</main>")[0]
                                                               .replace(bins, "")) == []
    more = (await client.get("/dashboard/more")).text
    assert 'href="/dashboard/jobs"' in more and "The bins, renewals and birthdays" in more


async def test_the_circle_ticks_a_job_off_however_early_and_a_row_can_be_skipped_or_stopped(client):
    async with tx() as conn:
        home = await family(conn)
    bins = await a_job(home, "Put the bins out", 6, at_time="19:00", rrule="FREQ=WEEKLY", owner="Ada")
    mot = await a_job(home, "MOT", 90, owner="Ola")
    headers = await login(client, home)
    (was,) = [job for job in await rows() if job["id"] == bins]

    assert (await client.post(f"/dashboard/jobs/{bins}/done")).status_code == 403            # no CSRF token
    done = await client.post(f"/dashboard/jobs/{bins}/done", headers=headers)
    assert done.status_code == 200 and "Last done on " in done.text and "by Ola" in done.text
    (job,) = [job for job in await rows() if job["id"] == bins]
    assert (clock(job["due_at"]), job["done_by"], job["status"]) == (
        clock(was["due_at"]) + timedelta(days=7), "Ola", "scheduled")

    await client.post(f"/dashboard/jobs/{bins}/skip", headers=headers)
    stopped = await client.post(f"/dashboard/jobs/{mot}/stop", headers=headers)
    assert '<span class="title">MOT</span>' not in stopped.text and "Put the bins out" in stopped.text
    by_id = {job["id"]: job for job in await rows()}
    assert clock(by_id[bins]["due_at"]) == clock(was["due_at"]) + timedelta(days=14)
    assert by_id[mot]["status"] == "cancelled"
    wrong = await client.post(f"/dashboard/jobs/{mot}/skip", headers=headers)
    assert 'role="alert">MOT was stopped' in wrong.text
    assert (await client.post(f"/dashboard/jobs/{bins}/eat", headers=headers)).text.count("unknown action") == 1
    actions = await rows("select tool, source, member_id from agent_actions where source = 'dashboard' order by created_at")
    assert [(a["tool"], a["member_id"]) for a in actions] == [
        ("jobs.done", home.ola), ("jobs.skip", home.ola), ("jobs.stop", home.ola)]            # each can be undone


async def test_today_shows_the_jobs_that_are_waiting_or_due_by_tomorrow_with_a_circle_to_tick(client):
    async with tx() as conn:
        home = await family(conn)
    pills = await a_job(home, "Ada's prescription", 0, owner="Ada", asked=True)
    await a_job(home, "Grandma's birthday", 1, kind="date")
    await a_job(home, "MOT", 30, owner="Ola")
    headers = await login(client, home)
    csrf = {"csrf": headers["X-CSRF-Token"]}

    page = (await client.get("/dashboard")).text
    assert "<h2>Jobs</h2>" in page and "Ada · due today" in page and "Everyone · tomorrow" in page
    assert "MOT" not in page and f'action="/dashboard/jobs/{pills}/done"' in page
    ticked = await client.post(f"/dashboard/jobs/{pills}/done", data=csrf)     # a plain form: back to Today
    assert (ticked.status_code, ticked.headers["location"]) == (303, "/dashboard")
    assert "Ada&#39;s prescription" not in (await client.get("/dashboard")).text
    (job,) = [job for job in await rows() if job["id"] == pills]
    assert (job["status"], job["done_by"]) == ("acked", "Ola")


async def test_a_job_cannot_be_reached_from_another_household(client):
    async with tx() as conn:
        home = await family(conn)
        other, sam = await households.create_household(conn, "Next door", "Europe/London", "Sam")
    theirs = await a_job(type(home)(other, sam, {"Ola": sam}), "Their bins", 1, rrule="FREQ=WEEKLY")
    headers = await login(client, home)
    page = await client.post(f"/dashboard/jobs/{theirs}/stop", headers=headers)
    assert "that job no longer exists" in page.text and "Their bins" not in page.text
    async with tx() as conn:
        with pytest.raises(ToolError):
            await household_jobs.meant(conn, home.id, "bins", None)
        assert await household_jobs.listing(conn, home.id) == []
    assert [job["status"] for job in await rows()] == ["scheduled"]
