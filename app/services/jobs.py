"""Household jobs: the bins, the MOT, a prescription, a birthday (ADR 0037). Shared by the agent
tools, the dashboard and the worker.

A job is a standalone reminder that is to be done, not only said: a `reminders` row with a
`kind`. It has an owner (`member_id`, or nobody for everyone), the occurrence it is waiting on
(`due_at`) and, when it comes round again, an RRULE. `fire_at` is when the worker next has
something to do for it, as it is for every reminder: say it is coming, say it is due, ask once
more the morning after, or begin the next occurrence. A job that repeats stays `scheduled`
through all of them, like a repeating reminder. One that does not ends `acked` when it is ticked
off, or `sent` once everything has been said and nobody has.

Not to be confused with app/worker/jobs.py, the worker's own scheduled jobs, one of which
(`nudge_household_jobs`) sends what this module decides.
"""
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.agent.actions import Recorder
from app.agent.base import ToolError
from app.core.envelope import OutboundMessage
from app.core.timeutil import day_bounds, local, next_occurrence, normalise_rrule, to_utc
from app.db import execute, fetch_all, fetch_one, fetch_val
from app.services import households, members

MORNING = time(9, 0)             # when a job due on a day is said, and when one not done is asked about again
LATER = timedelta(hours=2)       # "later" for a job with a time, on its day; otherwise it is the next morning
RENEWAL_LEAD_DAYS = 14           # a job due on a day that comes round rarely, or once, is also said this long before
DATE_LEAD_DAYS = 7               # and a birthday this long before
RARELY = timedelta(days=90)
MAX_LEAD_DAYS = 60
THRESHOLD = 0.3                  # how like a job's name a description must be, as for events
FILLER = {"the", "a", "an", "my", "our", "your", "job", "jobs"}   # words that say nothing about which job
KEY = "job"                      # outbox dedupe keys of nudges: job:{id}:{due}:{asked at}, times as epoch seconds
REPLY = "Reply done when it's sorted, or later to be asked again."

_JOB = """
select r.id, r.household_id, r.text, r.kind, r.status, r.fire_at, r.due_at, r.all_day, r.lead_days, r.rrule,
       r.asked_at, r.done_at, r.done_by, r.member_id, r.urgency, h.timezone,
       m.name as owner, m.role as owner_role, d.name as done_by_name,
       r.kind = 'job' and r.asked_at is not null as waiting
from reminders r join households h on h.id = r.household_id
left join members m on m.id = r.member_id left join members d on d.id = r.done_by"""
_OPEN = "r.kind is not null and r.status in ('scheduled', 'sent')"


# ---------------------------------------------------------------- when things are said
def _morning(day: date, timezone: str) -> datetime:
    return to_utc(datetime.combine(day, MORNING), timezone)


def asks(job: dict[str, Any], due: datetime | None = None) -> list[datetime]:
    """When one occurrence is spoken of, in order: ahead of it if the job has a lead, when it is
    due, and for a job (not a date) the morning after."""
    due, timezone = due or job["due_at"], job["timezone"]
    day = local(due, timezone).date()
    ahead = [_morning(day - timedelta(days=job["lead_days"]), timezone)] if job["lead_days"] else []
    after = [_morning(day + timedelta(days=1), timezone)] if job["kind"] == "job" else []
    return [*ahead, due, *after]


def following(job: dict[str, Any], due: datetime | None = None) -> datetime | None:
    """The occurrence after `due`; None for a job that does not come round again."""
    due = due or job["due_at"]
    return next_occurrence(job["rrule"], due, job["timezone"], due) if job["rrule"] else None


def _still_to_say(job: dict[str, Any], now: datetime) -> list[datetime]:
    """What is left to say about this occurrence before the next one's turn comes."""
    after = following(job)
    turn = asks(job, after)[0] if after else None
    return [at for at in asks(job) if at > now and (turn is None or at < turn)]


def _wake(job: dict[str, Any], now: datetime) -> datetime | None:
    """When the worker next has something to do for the job as it stands: the next thing to say
    about this occurrence, else the first thing to say about the next one."""
    left = _still_to_say(job, now)
    if left:
        return left[0]
    after = following(job)
    return asks(job, after)[0] if after else None


async def _recipient(conn: AsyncConnection, job: dict[str, Any]) -> tuple[str, str | None]:
    """The owner in their own chat, if they are an adult who is connected; otherwise everyone."""
    if job["member_id"] and job["owner_role"] == "adult" and await members.is_connected(conn, job["member_id"]):
        return "member", job["member_id"]
    return "household", None


async def _urgency(conn: AsyncConnection, job: dict[str, Any], at: datetime | None, *, chosen: bool = False) -> str:
    """A time somebody picked is meant, even inside quiet hours (ADR 0012): the job's own time of
    day, and a "later" that names a time. Everything else waits for quiet hours to end."""
    if at is None or not (chosen or (at == job["due_at"] and not job["all_day"])):
        return "normal"
    _, member_id = await _recipient(conn, job)
    return "high" if await members.quiet_until(conn, job["household_id"], member_id, at) is not None else "normal"


# ---------------------------------------------------------------- wording
def when(moment: datetime, timezone: str, all_day: bool | None = False, now: datetime | None = None) -> str:
    """'Tue 6 Oct at 19:00', or 'Sun 14 Mar' for a day with no time, with the year when it is not this one."""
    here = local(moment, timezone)
    day = f"{here:%a %-d %b}" + (f" {here:%Y}" if now and here.year != local(now, timezone).year else "")
    return day if all_day else f"{day} at {here:%H:%M}"


def due_words(job: dict[str, Any], now: datetime) -> str:
    """When the job is due as someone reading it at `now` would say it: 'today at 19:00', 'tomorrow',
    'yesterday', 'on Sun 14 Mar'."""
    due, today = local(job["due_at"], job["timezone"]), local(now, job["timezone"]).date()
    days = (due.date() - today).days
    day = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(days)
    day = day or "on " + when(job["due_at"], job["timezone"], True, now)
    return day if job["all_day"] else f"{day} at {due:%H:%M}"


def _countdown(days: int) -> str:
    if days < 2:
        return ""
    weeks = days // 7
    return f" (in {'a week' if weeks == 1 else f'{weeks} weeks'})" if days % 7 == 0 else f" (in {days} days)"


def nudge_text(job: dict[str, Any], now: datetime, *, last: bool, to_owner: bool) -> str:
    """What is said about a job at `now`. `last` is whether this is the last time this occurrence
    is spoken of; `to_owner` whether it goes to the owner alone, who need not be named."""
    title = job["text"]
    days = (local(job["due_at"], job["timezone"]).date() - local(now, job["timezone"]).date()).days
    due = due_words(job, now)
    if job["kind"] == "date":
        return f"Today: {title}." if days == 0 else f"Coming up: {title}, {due}{_countdown(days)}."
    if job["owner"] and not to_owner:
        title += f" ({job['owner']})"
    if days > 0:
        return f"Coming up: {title}, due {due}{_countdown(days)}. {REPLY}"
    start = f"Still to do: {title}, due {due}." if job["asked_at"] or days < 0 else f"To do {due}: {title}."
    if not last:
        return f"{start} {REPLY}"
    if job["rrule"]:
        return f"{start} Reply done if it's sorted. If not, I'll leave it until the next one."
    return f"{start} Reply done once it's sorted. I won't keep asking."


def summary(job: dict[str, Any], now: datetime) -> str:
    """One job on one line, for the brief and for a tool result: whose it is, when, how often."""
    whose = job["owner"] or "everyone"
    due = when(job["due_at"], job["timezone"], job["all_day"], now)
    state = f"asked, not done yet, was due {due}" if job["waiting"] else f"due {due}"
    if job["kind"] == "date":
        state = f"a date, {due}"
    return f"{job['text']} ({whose}, {state}{', repeats ' + job['rrule'] if job['rrule'] else ''})"


def upcoming_line(job: dict[str, Any]) -> str:
    """A job as `list_upcoming` shows it, beside events and reminders."""
    due = local(job["due_at"], job["timezone"])
    text = (f"{due:%a %-d %b} " if job["all_day"] else f"{due:%a %-d %b %H:%M} ") + f"{job['kind']}: {job['text']}"
    text += f" ({job['owner'] or 'everyone'})"
    return text + (" [asked, not done yet]" if job["waiting"] else "") + (" [repeats]" if job["rrule"] else "")


# ---------------------------------------------------------------- the worker's side
def nudge_key(job: dict[str, Any]) -> str:
    return f"{KEY}:{job['id']}:{int(job['due_at'].timestamp())}:{int(job['fire_at'].timestamp())}"


def named_in(key: str | None) -> dict[str, Any]:
    """The job and occurrence a sent nudge was about, read back from its outbox key. The router
    keeps it on the sent message, so an answer to that message can be matched to the job."""
    parts = (key or "").split(":")
    return {"job": parts[1], "due": int(parts[2])} if parts[0] == KEY and len(parts) >= 4 else {}


async def due_for_a_nudge(conn: AsyncConnection, now: datetime, limit: int = 100) -> list[dict[str, Any]]:
    return await fetch_all(
        conn, _JOB + """ where r.kind is not null and r.status = 'scheduled' and r.fire_at <= :now
                         order by r.fire_at, r.text, r.id for update of r skip locked limit :limit""",
        now=now, limit=limit)


async def nudge(conn: AsyncConnection, job: dict[str, Any], now: datetime) -> OutboundMessage | None:
    """One job whose time has come: what to send, if anything, with the row moved on to whatever
    comes next. The caller queues the message in the same transaction."""
    key, due = nudge_key(job), job["due_at"]
    while (after := following(job, due)) and asks(job, after)[0] <= now:
        due = after                       # the next one's turn has come; the one before was never ticked off
    if due != job["due_at"]:
        job = {**job, "due_at": due, "asked_at": None}
    left, after = _still_to_say(job, now), following(job)
    target, member_id = await _recipient(conn, job)
    stale = job["kind"] == "date" and local(due, job["timezone"]).date() < local(now, job["timezone"]).date()
    message = None if stale else OutboundMessage(
        household_id=job["household_id"], target=target, member_id=member_id, urgency=job["urgency"],
        text=nudge_text(job, now, last=not left, to_owner=target == "member"), dedupe_key=key)

    asked, status = job["asked_at"] or now, "scheduled"
    if job["kind"] == "date" and not left:    # the day itself has been said, and nothing is waited for
        if after:
            job, asked = {**job, "due_at": after}, None
        else:
            status = "sent"
    wake = _wake(job, now)
    if wake is None:
        status = "sent"                       # everything has been said; it waits to be ticked off
    await execute(
        conn,
        """update reminders set due_at = :due, asked_at = :asked, status = :status, urgency = :urgency,
                                fire_at = coalesce(:wake, fire_at),
                                sent_at = case when :status = 'sent' then :now else sent_at end
           where id = :id""",
        due=job["due_at"], asked=asked, status=status, wake=wake, now=now, id=job["id"],
        urgency=await _urgency(conn, job, wake))
    return message


# ---------------------------------------------------------------- reads
async def listing(conn: AsyncConnection, household_id: str, member_id: str | None = None) -> list[dict[str, Any]]:
    """Every open job, the ones waiting to be ticked off first, then by when they are due. With
    `member_id`, that person's and everyone's."""
    return await fetch_all(
        conn, _JOB + f""" where r.household_id = :h and {_OPEN}
                            and (cast(:member as uuid) is null or r.member_id is null or r.member_id = :member)
                          order by waiting desc, r.due_at, r.text""", h=household_id, member=member_id)


async def upcoming(conn: AsyncConnection, household_id: str, end: datetime,
                   member_id: str | None = None) -> list[dict[str, Any]]:
    """Open jobs due before `end`, with the ones still waiting from earlier."""
    return [job for job in await listing(conn, household_id, member_id) if job["due_at"] < end]


async def find(conn: AsyncConnection, household_id: str, name: str) -> list[dict[str, Any]]:
    """Open jobs that could be the one a person calls `name`, the likeliest first. "The car" must
    not find "Put the bins out" on the strength of "the", so filler words are left out."""
    key = " ".join(word for word in name.lower().split() if word not in FILLER)
    if not key:
        return []
    rows = await fetch_all(
        conn,
        f"""select r.id, r.text, r.kind = 'job' and r.asked_at is not null as waiting,
                   greatest(similarity(lower(r.text || ' ' || coalesce(m.name, '')), :key),
                            word_similarity(lower(r.text), :key),
                            word_similarity(:key, lower(r.text || ' ' || coalesce(m.name, '')))) as score
            from reminders r left join members m on m.id = r.member_id
            where r.household_id = :h and {_OPEN} order by score desc, waiting desc, r.due_at, r.text""",
        h=household_id, key=key)
    return [row for row in rows if row["score"] >= THRESHOLD]


async def answered(conn: AsyncConnection, message_id: str | None) -> dict[str, Any] | None:
    """The nudge a message replies to or reacts to: its job, that occurrence, and the message."""
    if message_id is None:
        return None
    nudge_sent = await fetch_one(
        conn,
        """select o.id as message_id, o.meta->>'job' as job_id, o.meta->>'due' as due
           from messages m join messages o on o.thread_id = m.thread_id and o.direction = 'out'
             and o.external_id in (m.meta->>'reply_to_external_id', m.meta->>'reaction_target_external_id')
           where m.id = :id and o.meta ? 'job'""", id=message_id)
    return nudge_sent and {**nudge_sent, "due": datetime.fromtimestamp(int(nudge_sent["due"]), UTC)}


async def meant(conn: AsyncConnection, household_id: str, name: str | None, message_id: str | None) -> str:
    """The id of the job a person means. By name if they gave one; otherwise the one whose nudge
    their message answers, or the only one waiting to be ticked off. Raises ToolError when it
    cannot tell, listing what there is."""
    jobs = await listing(conn, household_id)
    names = ", ".join(job["text"] for job in jobs) or "none yet"
    if name and name.strip():
        found = await find(conn, household_id, name)
        if not found:
            raise ToolError(f"no job is called anything like '{name}'. The jobs: {names}")
        best = found[0]
        close = [job for job in found if job["score"] >= best["score"] - 0.05 and job["waiting"] == best["waiting"]]
        if len(close) > 1:
            raise ToolError(f"'{name}' could be {', '.join(job['text'] for job in close)}. "
                            "Ask which, in one short question")
        return str(found[0]["id"])
    replied_to = await answered(conn, message_id)
    if replied_to and any(job["id"] == replied_to["job_id"] for job in jobs):
        return str(replied_to["job_id"])
    waiting = [job for job in jobs if job["waiting"]]
    if len(waiting) != 1:
        raise ToolError("say which job. Waiting to be ticked off: "
                        f"{', '.join(job['text'] for job in waiting) or 'none'}. All the jobs: {names}")
    return str(waiting[0]["id"])


# ---------------------------------------------------------------- writes
async def _job(rec: Recorder, job_id: str, *, open_only: bool = True) -> dict[str, Any]:
    job = await fetch_one(rec.ctx.conn, _JOB + " where r.id = :id and r.household_id = :h and r.kind is not null "
                                               "for update of r", id=job_id, h=rec.ctx.household_id)
    if job is None:
        raise ToolError("that job no longer exists")
    if open_only and job["status"] not in ("scheduled", "sent"):
        raise ToolError(f"{job['text']} was already ticked off" if job["status"] == "acked"
                        else f"{job['text']} was stopped")
    return job


def _planned(timezone: str, now: datetime, *, due_on: date, at_time: time | None, rrule: str | None, kind: str,
             lead_days: int | None) -> dict[str, Any]:
    """The first occurrence that has not passed, the rule as it is stored, and how long before to say it."""
    all_day = at_time is None
    due = to_utc(datetime.combine(due_on, at_time or MORNING), timezone)
    # A day with no time is still due for the whole of that day.
    earliest = day_bounds(local(now, timezone).date(), timezone)[0] if all_day else now
    if rrule:
        if "COUNT" in rrule.upper():
            raise ToolError("give the repeat an end date (UNTIL) instead of COUNT")
        try:
            rrule = normalise_rrule(rrule, due, timezone)
        except ValueError as exc:
            raise ToolError(f"the repeat rule is not valid RFC 5545 ({exc})") from None
        first = next_occurrence(rrule, due, timezone, max(earliest, due) - timedelta(seconds=1))
        if first is None:
            raise ToolError("that repeat never comes round again")
        due = first
    elif due < earliest:
        raise ToolError(f"{when(due, timezone, all_day, now)} has already passed; "
                        f"it is now {local(now, timezone):%a %-d %b %Y %H:%M}")
    after = next_occurrence(rrule, due, timezone, due) if rrule else None
    if lead_days is None:
        rarely = after is None or after - due >= RARELY
        lead_days = DATE_LEAD_DAYS if kind == "date" else RENEWAL_LEAD_DAYS if all_day and rarely else 0
    if not 0 <= lead_days <= MAX_LEAD_DAYS:
        raise ToolError(f"a job can be said at most {MAX_LEAD_DAYS} days before it is due")
    if after is not None:
        lead_days = min(lead_days, max((after - due).days - 1, 0))   # never before the one before it
    return {"due_at": due, "all_day": all_day, "rrule": rrule or None, "lead_days": lead_days}


def _described(job: dict[str, Any], now: datetime) -> str:
    text = f"{job['text']} for {job['owner'] or 'everyone'}, "
    text += ("on " if job["kind"] == "date" else "due ") + when(job["due_at"], job["timezone"], job["all_day"], now)
    return text + (f", repeating ({job['rrule']})" if job["rrule"] else "")


def _plan_note(job: dict[str, Any]) -> str:
    if job["status"] != "scheduled":
        return "NOTE: it is today, so there is nothing left to say about it"
    first = when(job["fire_at"], job["timezone"])
    if job["kind"] == "date":
        return f"NOTE: nothing to tick off; it is said on {first}" + (" and on the day" if job["lead_days"] else "")
    return f"NOTE: first nudge on {first}; it is ticked off when someone says done"


async def add(rec: Recorder, *, title: str, due_on: date, at_time: time | None = None, rrule: str | None = None,
              member_id: str | None = None, kind: str = "job", lead_days: int | None = None) -> str:
    """A new job, or a date to remember. No `member_id` means it is everyone's. Returns its id."""
    ctx = rec.ctx
    timezone = await households.timezone(ctx.conn, ctx.household_id)
    title = " ".join(title.split())
    if not title:
        raise ToolError("say what the job is")
    if kind not in ("job", "date"):
        raise ToolError("a job or a date")
    if kind == "date" and not rrule:
        rrule = "FREQ=YEARLY"      # a birthday comes round every year whether or not anyone says so
    plan = _planned(timezone, ctx.now, due_on=due_on, at_time=at_time, rrule=rrule, kind=kind, lead_days=lead_days)
    existing = await fetch_one(
        ctx.conn, _JOB + f""" where r.household_id = :h and {_OPEN} and lower(r.text) = lower(:title)
                               and r.due_at = :due and r.member_id is not distinct from :member""",
        h=ctx.household_id, title=title, due=plan["due_at"], member=member_id)
    if existing is not None:
        rec.lines.append(f"OK: {_described(existing, ctx.now)} is already set")
        return str(existing["id"])

    owner = await fetch_one(ctx.conn, "select name, role from members where id = :id and household_id = :h",
                            id=member_id, h=ctx.household_id) if member_id else None
    if member_id and owner is None:
        raise ToolError("nobody like that is in the family")
    job: dict[str, Any] = {
        **plan, "household_id": ctx.household_id, "timezone": timezone, "kind": kind, "text": title,
        "member_id": member_id, "owner": owner and owner["name"], "owner_role": owner and owner["role"],
        "asked_at": None,
    }
    wake = _wake(job, ctx.now)
    job |= {"fire_at": wake or plan["due_at"], "status": "scheduled" if wake else "sent"}
    job_id = str(await fetch_val(
        ctx.conn,
        """insert into reminders (household_id, target, member_id, text, fire_at, rrule, urgency, status, kind,
                                  due_at, all_day, lead_days)
           values (:h, :target, :member, :text, :fire_at, :rrule, :urgency, :status, :kind, :due_at, :all_day,
                   :lead_days) returning id""",
        h=ctx.household_id, target="member" if member_id else "household", member=member_id, text=title,
        fire_at=job["fire_at"], rrule=job["rrule"], urgency=await _urgency(ctx.conn, job, wake), status=job["status"],
        kind=kind, due_at=job["due_at"], all_day=job["all_day"], lead_days=job["lead_days"]))
    rec.created("reminders", job_id)
    rec.lines += [f"OK: {_described(job, ctx.now)}", _plan_note(job)]
    return job_id


async def _move_on(rec: Recorder, job: dict[str, Any], after: datetime, **also: Any) -> None:
    """Leave this occurrence behind and wait on the next."""
    ahead = {**job, "due_at": after, "asked_at": None}
    wake = _wake(ahead, rec.ctx.now) or after
    await execute(
        rec.ctx.conn,
        """update reminders set due_at = :due, asked_at = null, fire_at = :wake, status = 'scheduled',
                                urgency = :urgency, done_at = coalesce(:done_at, done_at),
                                done_by = coalesce(:done_by, done_by)
           where id = :id""",
        due=after, wake=wake, urgency=await _urgency(rec.ctx.conn, ahead, wake), id=job["id"],
        done_at=also.get("done_at"), done_by=also.get("done_by"))


async def done(rec: Recorder, job_id: str, *, occurrence: datetime | None = None, sure: bool = False) -> bool:
    """Tick a job off; one that repeats then waits on its next occurrence. Returns whether anything
    changed. `occurrence` is the one a ticked nudge was about: if the job has moved on from it,
    nothing changes. Without `sure`, a job that was not asked about yet and is not due for more
    than half its cycle is left alone: that is somebody saying again what was already ticked off."""
    ctx = rec.ctx
    job = await _job(rec, job_id, open_only=False)
    title, timezone = job["text"], job["timezone"]
    if job["status"] not in ("scheduled", "sent") or (occurrence is not None and occurrence != job["due_at"]):
        rec.lines.append(f"OK: {title} was stopped" if job["status"] == "cancelled"
                         else f"OK: {title} was already ticked off")
        return False
    if job["kind"] == "date":
        raise ToolError(f"{title} is a date, not a job: there is nothing to tick off")
    after = following(job)
    if after and not sure and not job["asked_at"] and job["due_at"] - ctx.now > (after - job["due_at"]) / 2:
        before = f"was ticked off on {when(job['done_at'], timezone)}" if job["done_at"] else "is not due yet"
        rec.lines.append(f"OK: nothing to tick off: {title} {before}, and is next due "
                         f"{when(job['due_at'], timezone, job['all_day'], ctx.now)}")
        return False
    await rec.before("reminders", id=job_id)
    if after is None:
        await execute(ctx.conn, "update reminders set status = 'acked', done_at = :now, done_by = :by where id = :id",
                      now=ctx.now, by=ctx.member_id, id=job_id)
        rec.lines.append(f"OK: {title} done")
    else:
        await _move_on(rec, job, after, done_at=ctx.now, done_by=ctx.member_id)
        rec.lines.append(f"OK: {title} done. Next: {when(after, timezone, job['all_day'], ctx.now)}")
    return True


async def later(rec: Recorder, job_id: str, until: datetime | None = None) -> None:
    """Ask again later: at `until`, else in two hours for a job with a time on its day, else the
    next morning. Never later than the next occurrence's own nudge, which is not to be missed."""
    ctx = rec.ctx
    job = await _job(rec, job_id)
    timezone = job["timezone"]
    if job["kind"] == "date":
        raise ToolError(f"{job['text']} is a date: it is only said ahead of the day and on it")
    here = local(ctx.now, timezone)
    if until is None:
        today = not job["all_day"] and local(job["due_at"], timezone).date() == here.date()
        until = ctx.now + LATER if today else _morning(here.date() + timedelta(days=1), timezone)
    elif until <= ctx.now:
        raise ToolError(f"{when(until, timezone)} has already passed; it is now {here:%a %-d %b %Y %H:%M}")
    after = following(job)
    if after is not None:
        until = min(until, asks(job, after)[0])
    await rec.before("reminders", id=job_id)
    await execute(
        ctx.conn, "update reminders set fire_at = :until, status = 'scheduled', urgency = :urgency where id = :id",
        until=until, id=job_id, urgency=await _urgency(ctx.conn, job, until, chosen=True))
    rec.lines.append(f"OK: will ask again about {job['text']} on {when(until, timezone)}")


async def skip(rec: Recorder, job_id: str) -> None:
    """Not this time: the next occurrence stands."""
    job = await _job(rec, job_id)
    after = following(job)
    if after is None:
        raise ToolError(f"{job['text']} does not come round again: stop it instead")
    await rec.before("reminders", id=job_id)
    await _move_on(rec, job, after)
    said = [when(at, job["timezone"], job["all_day"], rec.ctx.now) for at in (job["due_at"], after)]
    rec.lines.append(f"OK: {job['text']} skipped for {said[0]}. Next: {said[1]}")


async def stop(rec: Recorder, job_id: str) -> None:
    job = await _job(rec, job_id)
    await rec.before("reminders", id=job_id)
    await execute(rec.ctx.conn, "update reminders set status = 'cancelled' where id = :id", id=job_id)
    rec.lines.append(f"OK: {job['text']} stopped")


async def change(rec: Recorder, job_id: str, *, title: str | None = None, member_id: str | None = None,
                 everyone: bool = False, due_on: date | None = None, at_time: time | None = None,
                 rrule: str | None = None, lead_days: int | None = None) -> None:
    """A new name, owner, day, time, repeat or lead. What is not given stays as it is; a new day,
    time or repeat starts the job on its next occurrence afresh."""
    ctx = rec.ctx
    job = await _job(rec, job_id)
    timezone = job["timezone"]
    name = " ".join((title or "").split()) or job["text"]
    owner_id = None if everyone else member_id or job["member_id"]
    owner = await fetch_one(ctx.conn, "select name, role from members where id = :id and household_id = :h",
                            id=owner_id, h=ctx.household_id) if owner_id else None
    if owner_id and owner is None:
        raise ToolError("nobody like that is in the family")
    changed: dict[str, Any] = {**job, "text": name, "member_id": owner_id, "owner": owner and owner["name"],
                               "owner_role": owner and owner["role"]}
    if not (title or member_id or everyone or due_on or at_time or rrule or lead_days is not None):
        raise ToolError("say what to change: the name, whose it is, the day, the time or how often")
    if due_on or at_time or rrule or lead_days is not None:
        was = local(job["due_at"], timezone)
        changed |= _planned(
            timezone, ctx.now, due_on=due_on or max(was.date(), local(ctx.now, timezone).date()),
            at_time=at_time or (None if job["all_day"] else was.time().replace(tzinfo=None)),
            rrule=rrule or job["rrule"], kind=job["kind"],
            lead_days=lead_days if lead_days is not None else (None if due_on or rrule else job["lead_days"]))
        if changed["due_at"] != job["due_at"]:
            changed["asked_at"] = None
    # Only a change to when it is due moves when it is next said: a new owner or name keeps a "later".
    retimed = any(changed[name] != job[name] for name in ("due_at", "all_day", "rrule", "lead_days"))
    wake = _wake(changed, ctx.now) if retimed else None
    await rec.before("reminders", id=job_id)
    await execute(
        ctx.conn,
        """update reminders set text = :text, member_id = :member, target = :target, due_at = :due, all_day = :all_day,
                                rrule = :rrule, lead_days = :lead_days, asked_at = :asked,
                                fire_at = coalesce(:wake, fire_at), urgency = :urgency,
                                status = case when cast(:wake as timestamptz) is null then status else 'scheduled' end
           where id = :id""",
        text=name, member=owner_id, target="member" if owner_id else "household", due=changed["due_at"],
        all_day=changed["all_day"], rrule=changed["rrule"], lead_days=changed["lead_days"], asked=changed["asked_at"],
        wake=wake, urgency=await _urgency(ctx.conn, changed, wake or job["fire_at"]), id=job_id)
    rec.lines.append(f"OK: now {_described(changed, ctx.now)}")
