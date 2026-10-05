"""Calendar write path, shared by agent tools and the dashboard (spec sections 9 and 9.2).

Events and reminders are only written here. Each write goes through a Recorder, so undo
restores an event and its reminders exactly. Times are UTC; the household time zone is
used only to expand recurrences and to word reminders.
"""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.agent.actions import Recorder
from app.agent.base import ToolError
from app.core.timeutil import day_bounds, local, next_occurrence, normalise_rrule, occurrences
from app.db import execute, fetch_all, fetch_one, fetch_val
from app.services import households, members

DAY_MINUTES = 1440
DEFAULT_LEADS = (DAY_MINUTES, 60)
MAX_LEAD_MINUTES = 7 * DAY_MINUTES
HORIZON = timedelta(hours=48)   # how far ahead a recurring event has reminder rows

_EVENT = """
select e.*, h.timezone,
       coalesce((select array_agg(m.name order by m.created_at) from members m
                 where m.id = any(e.participant_ids)), '{}') as participants
from events e join households h on h.id = e.household_id"""


# ---------------------------------------------------------------- wording
def label(event: dict[str, Any]) -> str:
    """The title with the people it is for, unless the title already names them."""
    names = [name for name in event["participants"] if name.lower() not in event["title"].lower()]
    return f"{event['title']} for {' and '.join(names)}" if names else event["title"]


def describe(event: dict[str, Any], start: datetime | None = None) -> str:
    begins = local(start or event["starts_at"], event["timezone"])
    text = f"{label(event)} on {begins:%a %-d %b} at {begins:%H:%M}"
    return f"{text}, {event['location']}" if event["location"] else text


def reminder_text(event: dict[str, Any], title: str, start: datetime, read_at: datetime) -> str:
    """`{title} {relative day} at {HH:MM}{, location}`, relative to when it will be read."""
    begins, reading = local(start, event["timezone"]), local(read_at, event["timezone"])
    days = (begins.date() - reading.date()).days
    day = "today" if days == 0 else "tomorrow" if days == 1 else f"on {begins:%a %-d %b}"
    text = f"{title} {day} at {begins:%H:%M}"
    return f"{text}, {event['location']}" if event["location"] else text


def _lead(minutes: int) -> str:
    for size, unit in ((DAY_MINUTES, "day"), (60, "hour"), (1, "minute")):
        if minutes >= size and minutes % size == 0:
            return f"{minutes // size} {unit}{'' if minutes == size else 's'}"
    return "0 minutes"


def _times(moments: Sequence[datetime], timezone: str) -> str:
    return ", ".join(f"{local(moment, timezone):%a %-d %b %H:%M}" for moment in moments)


# ---------------------------------------------------------------- reminder planning
async def _event(conn: AsyncConnection, household_id: str, event_id: str) -> dict[str, Any]:
    event = await fetch_one(conn, _EVENT + " where e.id = :id and e.household_id = :h for update of e",
                            id=event_id, h=household_id)
    if event is None:
        raise ToolError("that event no longer exists")
    return event


async def _target(conn: AsyncConnection, event: dict[str, Any]) -> tuple[str, str | None]:
    """The household if a child or more than one adult takes part, else that one adult."""
    people = await fetch_all(conn, "select id, role from members where id = any(cast(:ids as uuid[]))",
                             ids=[str(member) for member in event["participant_ids"]])
    adults = [person["id"] for person in people if person["role"] == "adult"]
    if len(adults) < len(people) or len(adults) > 1:
        return "household", None
    if adults:
        return "member", adults[0]
    return ("member", event["created_by"]) if event["created_by"] else ("household", None)


async def _planned(conn: AsyncConnection, event: dict[str, Any], now: datetime) -> dict[datetime, dict[str, Any]]:
    """The reminder rows the event should have from `now` on, keyed by fire time."""
    if event["status"] != "active":
        return {}
    leads: list[int] = event["remind_before_minutes"]
    if event["rrule"]:
        horizon = now + max(HORIZON, timedelta(minutes=max(leads, default=0), hours=2))
        starts = occurrences(event["rrule"], event["starts_at"], event["timezone"], now, horizon, event["exdates"])
    else:
        starts = [event["starts_at"]]
    target, member_id = await _target(conn, event)
    title = event["title"] if target == "member" else label(event)
    planned: dict[datetime, dict[str, Any]] = {}
    for start in starts:
        for lead in leads:
            fire_at = start - timedelta(minutes=lead)
            if fire_at <= now:
                continue
            urgency, read_at = "normal", fire_at
            held_until = await members.quiet_until(conn, event["household_id"], member_id, fire_at)
            if held_until is not None:
                if lead >= DAY_MINUTES:
                    continue                 # the morning brief carries it instead (spec 9.2)
                if held_until >= start:
                    urgency = "high"         # held back, it would arrive after the event began
                else:
                    read_at = held_until
            planned[fire_at] = {"target": target, "member_id": member_id, "urgency": urgency,
                                "text": reminder_text(event, title, start, read_at)}
    return planned


async def _insert_reminder(conn: AsyncConnection, event: dict[str, Any], fire_at: datetime,
                           plan: dict[str, Any]) -> str | None:
    reminder_id = await fetch_val(
        conn,
        """insert into reminders (household_id, event_id, target, member_id, text, fire_at, urgency)
           values (:h, :event, :target, :member_id, :text, :fire_at, :urgency)
           on conflict (event_id, fire_at) do nothing returning id""",
        h=event["household_id"], event=event["id"], fire_at=fire_at, **plan,
    )
    return None if reminder_id is None else str(reminder_id)


async def materialise(conn: AsyncConnection, household_id: str, event_id: str, now: datetime) -> int:
    """Insert the reminder rows a recurring event is missing. Idempotent: the hourly job calls it."""
    event = await _event(conn, household_id, event_id)
    planned = await _planned(conn, event, now)
    return sum([await _insert_reminder(conn, event, at, plan) is not None for at, plan in planned.items()])


async def _sync_reminders(rec: Recorder, event: dict[str, Any]) -> list[datetime]:
    """Make the event's scheduled reminders match the plan. Returns the fire times that now stand."""
    conn = rec.ctx.conn
    planned = await _planned(conn, event, rec.ctx.now)
    scheduled = await fetch_all(
        conn, "select id, fire_at from reminders where event_id = :e and status = 'scheduled' for update",
        e=event["id"])
    for row in scheduled:
        await rec.before("reminders", id=row["id"])
        plan = planned.get(row["fire_at"])
        if plan is None:
            await execute(conn, "delete from reminders where id = :id", id=row["id"])
        else:
            await execute(
                conn,
                """update reminders set text = :text, urgency = :urgency, target = :target, member_id = :member_id
                   where id = :id""", id=row["id"], **plan)
    kept = {row["fire_at"] for row in scheduled}
    for fire_at, plan in planned.items():
        if fire_at not in kept and (reminder_id := await _insert_reminder(conn, event, fire_at, plan)):
            rec.created("reminders", reminder_id)
    return sorted(planned)


async def _cancel_reminders(rec: Recorder, event_id: str, fire_times: list[datetime] | None = None) -> None:
    rows = await fetch_all(
        rec.ctx.conn,
        """select id from reminders where event_id = :e and status = 'scheduled'
             and (cast(:times as timestamptz[]) is null or fire_at = any(cast(:times as timestamptz[])))
           for update""",
        e=event_id, times=fire_times,
    )
    for row in rows:
        await rec.before("reminders", id=row["id"])
    await execute(rec.ctx.conn, "update reminders set status = 'cancelled' where id = any(cast(:ids as uuid[]))",
                  ids=[row["id"] for row in rows])


def _rule(rule: str, dtstart: datetime, timezone: str) -> str:
    try:
        return normalise_rrule(rule, dtstart, timezone)
    except ValueError as exc:
        raise ToolError(f"the repeat rule is not valid RFC 5545 ({exc})") from None


# ---------------------------------------------------------------- events
async def schedule_event(
    rec: Recorder, *, title: str, starts_at: datetime, kind: str = "appointment", ends_at: datetime | None = None,
    rrule: str | None = None, participant_ids: Sequence[str] = (), location: str | None = None,
    remind_before_minutes: Sequence[int] = DEFAULT_LEADS, notes: str | None = None,
) -> str:
    """Create an event and its reminders. Returns the event id."""
    ctx = rec.ctx
    timezone = await households.timezone(ctx.conn, ctx.household_id)
    title = title.strip()
    if not title:
        raise ToolError("the event needs a title")
    leads = sorted({int(lead) for lead in remind_before_minutes}, reverse=True)
    if any(not 0 <= lead <= MAX_LEAD_MINUTES for lead in leads):
        raise ToolError("a reminder can be at most a week before the event")
    duration = None if ends_at is None else ends_at - starts_at
    if duration is not None and duration <= timedelta(0):
        raise ToolError("the end must be after the start")
    if rrule:
        rrule = _rule(rrule, starts_at, timezone)
        # The series starts on its first real occurrence, so the stored start matches the rule.
        first = next_occurrence(rrule, starts_at, timezone, starts_at - timedelta(seconds=1))
        if first is None:
            raise ToolError("that repeat rule never happens")
        starts_at = first
    elif starts_at <= ctx.now:
        raise ToolError(f"{local(starts_at, timezone):%a %-d %b %Y %H:%M} has already passed; "
                        f"it is now {local(ctx.now, timezone):%a %-d %b %Y %H:%M}")

    existing = await fetch_one(
        ctx.conn,
        _EVENT + """ where e.household_id = :h and e.status = 'active' and lower(e.title) = lower(:title)
                       and e.starts_at = :starts and e.rrule is not distinct from :rrule""",
        h=ctx.household_id, title=title, starts=starts_at, rrule=rrule,
    )
    if existing is not None:
        rec.lines.append(f"OK: {describe(existing)} is already on the calendar")
        return str(existing["id"])

    event_id = str(await fetch_val(
        ctx.conn,
        """insert into events (household_id, title, kind, starts_at, ends_at, rrule, location, participant_ids,
                               remind_before_minutes, notes, source_message_id, created_by, created_at)
           values (:h, :title, :kind, :starts, :ends, :rrule, :location, cast(:participants as uuid[]),
                   :leads, :notes, :message, :member, clock_timestamp()) returning id""",
        h=ctx.household_id, title=title, kind=kind, starts=starts_at,
        ends=None if duration is None else starts_at + duration, rrule=rrule, location=location or None,
        participants=list(participant_ids), leads=leads, notes=notes or None, message=ctx.message_id,
        member=ctx.member_id,
    ))
    rec.created("events", event_id)
    event = await _event(ctx.conn, ctx.household_id, event_id)
    fire_times = await _sync_reminders(rec, event)
    if rrule:
        rec.lines.append(f"OK: {describe(event)}, repeating ({rrule})")
        if leads:
            rec.lines.append(f"NOTE: reminders {' and '.join(_lead(lead) for lead in leads)} before each one")
    else:
        rec.lines.append(f"OK: {describe(event)}")
        rec.lines.append(f"NOTE: reminders at {_times(fire_times, timezone)}" if fire_times
                         else "NOTE: no reminders, it is too close")
    return event_id


async def modify_event(
    rec: Recorder, event_id: str, *, cancel: bool = False, scope: str = "this", occurrence: date | None = None,
    starts_at: datetime | None = None, ends_at: datetime | None = None, title: str | None = None,
    location: str | None = None, participant_ids: Sequence[str] | None = None, notes: str | None = None,
) -> None:
    """Move, edit or cancel. On a recurring event, scope `this` changes one occurrence only."""
    ctx = rec.ctx
    event = await _event(ctx.conn, ctx.household_id, event_id)
    if event["status"] != "active":
        raise ToolError(f"{event['title']} was already cancelled")
    changes: dict[str, Any] = {
        name: value for name, value in {
            "starts_at": starts_at, "ends_at": ends_at, "title": (title or "").strip() or None,
            "participant_ids": None if participant_ids is None else list(participant_ids),
        }.items() if value is not None
    }
    for name, text in (("location", location), ("notes", notes)):
        if text is not None:
            changes[name] = text.strip() or None      # an empty string clears it
    if not cancel and not changes:
        raise ToolError("say what to change: a new time, title, place or people, or cancel")
    if event["rrule"] and scope == "this":
        await _change_one(rec, event, occurrence, cancel, changes)
        return

    await rec.before("events", id=event_id)
    if cancel:
        await execute(ctx.conn, "update events set status = 'cancelled' where id = :id", id=event_id)
        await _cancel_reminders(rec, event_id)
        rec.lines.append(f"OK: {label(event)} cancelled" + (", the whole series" if event["rrule"] else ""))
        return

    start = changes.get("starts_at", event["starts_at"])
    if event["rrule"] and "starts_at" in changes:
        start = next_occurrence(event["rrule"], start, event["timezone"], start - timedelta(seconds=1)) or start
        changes["starts_at"] = start
    if "ends_at" not in changes and event["ends_at"] and "starts_at" in changes:
        changes["ends_at"] = start + (event["ends_at"] - event["starts_at"])
    if changes.get("ends_at") and changes["ends_at"] <= start:
        raise ToolError("the end must be after the start")
    assignments = ", ".join(
        f"{column} = cast(:{column} as uuid[])" if column == "participant_ids" else f"{column} = :{column}"
        for column in changes
    )
    await execute(ctx.conn, f"update events set {assignments} where id = :id", id=event_id, **changes)
    event = await _event(ctx.conn, ctx.household_id, event_id)
    fire_times = await _sync_reminders(rec, event)
    if event["rrule"]:
        rec.lines.append(f"OK: now {describe(event)}, repeating ({event['rrule']})")
    else:
        rec.lines.append(f"OK: now {describe(event)}")
        rec.lines.append(f"NOTE: reminders at {_times(fire_times, event['timezone'])}" if fire_times
                         else "NOTE: no reminders, it is too close")


async def _change_one(rec: Recorder, event: dict[str, Any], day: date | None, cancel: bool,
                      changes: dict[str, Any]) -> None:
    """One occurrence of a series: skip its date, and add a one-off event if it moved or changed."""
    ctx, timezone = rec.ctx, event["timezone"]
    if day is None:
        start = next_occurrence(event["rrule"], event["starts_at"], timezone, ctx.now, event["exdates"])
    else:
        on_day = occurrences(event["rrule"], event["starts_at"], timezone, *day_bounds(day, timezone),
                             event["exdates"])
        start = on_day[0] if on_day else None
    if start is None:
        raise ToolError(f"{event['title']} has no occurrence " + ("left" if day is None else f"on {day:%a %-d %b}"))

    await rec.before("events", id=event["id"])
    await execute(ctx.conn, "update events set exdates = array_append(exdates, :day) where id = :id",
                  day=local(start, timezone).date(), id=event["id"])
    await _cancel_reminders(
        rec, event["id"], [start - timedelta(minutes=lead) for lead in event["remind_before_minutes"]])
    if cancel:
        rec.lines.append(f"OK: {describe(event, start)} cancelled; the other dates stand")
        return
    moved_to = changes.get("starts_at", start)
    ends_at = changes.get("ends_at")
    if ends_at is None and event["ends_at"]:
        ends_at = moved_to + (event["ends_at"] - event["starts_at"])
    await schedule_event(
        rec, title=changes.get("title", event["title"]), kind=event["kind"], starts_at=moved_to, ends_at=ends_at,
        participant_ids=changes.get("participant_ids", [str(member) for member in event["participant_ids"]]),
        location=changes.get("location", event["location"]), remind_before_minutes=event["remind_before_minutes"],
        notes=changes.get("notes", event["notes"]),
    )
    rec.lines.append(f"NOTE: only {local(start, timezone):%a %-d %b} changed; the other dates stand")


# ---------------------------------------------------------------- standalone reminders
async def set_reminder(rec: Recorder, *, text: str, fire_at: datetime | None = None, rrule: str | None = None,
                       member_id: str | None = None, urgent: bool = False) -> None:
    """A reminder with no event: one-off at `fire_at`, or repeating. No `member_id` means the household."""
    ctx = rec.ctx
    timezone = await households.timezone(ctx.conn, ctx.household_id)
    text = text.strip()
    if not text:
        raise ToolError("say what the reminder is for")
    if rrule:
        if "COUNT" in rrule.upper():
            raise ToolError("give the repeat an end date (UNTIL) instead of COUNT")
        if fire_at is None and "BYHOUR" not in rrule.upper():
            raise ToolError("say what time of day: give fire_at for the first one")
        anchor = fire_at or local(ctx.now, timezone).replace(minute=0, second=0, microsecond=0)
        rrule = _rule(rrule, anchor, timezone)
        first = next_occurrence(rrule, anchor, timezone, max(ctx.now, anchor - timedelta(seconds=1)))
        if first is None:
            raise ToolError("that repeat never comes round again")
    elif fire_at is None:
        raise ToolError("give fire_at for a one-off or rrule for a repeat")
    elif fire_at <= ctx.now:
        raise ToolError(f"{local(fire_at, timezone):%a %-d %b %Y %H:%M} has already passed; "
                        f"it is now {local(ctx.now, timezone):%a %-d %b %Y %H:%M}")
    else:
        first = fire_at

    when = f"{local(first, timezone):%a %-d %b %H:%M}"
    if await fetch_val(
        ctx.conn,
        """select 1 from reminders where household_id = :h and event_id is null and status = 'scheduled'
             and lower(text) = lower(:text) and fire_at = :first""", h=ctx.household_id, text=text, first=first,
    ):
        rec.lines.append(f"OK: that reminder is already set for {when}")
        return
    # A time the user picked inside quiet hours is meant: it goes out then (spec section 10).
    explicit = urgent or await members.quiet_until(ctx.conn, ctx.household_id, member_id, first) is not None
    reminder_id = await fetch_val(
        ctx.conn,
        """insert into reminders (household_id, target, member_id, text, fire_at, rrule, urgency)
           values (:h, :target, :member, :text, :first, :rrule, :urgency) returning id""",
        h=ctx.household_id, target="member" if member_id else "household", member=member_id, text=text,
        first=first, rrule=rrule, urgency="high" if explicit else "normal",
    )
    rec.created("reminders", str(reminder_id))
    rec.lines.append(f"OK: repeating reminder ({rrule}), first on {when}: {text}" if rrule
                     else f"OK: reminder set for {when}: {text}")


async def cancel_reminder(rec: Recorder, reminder_id: str) -> None:
    row = await fetch_one(
        rec.ctx.conn,
        """select id, text from reminders where id = :id and household_id = :h and event_id is null
             and status = 'scheduled' for update""", id=reminder_id, h=rec.ctx.household_id)
    if row is None:
        raise ToolError("that reminder is no longer scheduled")
    await rec.before("reminders", id=reminder_id)
    await execute(rec.ctx.conn, "update reminders set status = 'cancelled' where id = :id", id=reminder_id)
    rec.lines.append(f"OK: reminder cancelled: {row['text']}")


# ---------------------------------------------------------------- reads
@dataclass(frozen=True)
class Occurrence:
    event_id: str
    title: str
    start: datetime
    location: str | None
    participants: list[str]
    rrule: str | None

    def line(self, timezone: str, *, day: bool = True) -> str:
        text = f"{local(self.start, timezone):{'%a %-d %b %H:%M' if day else '%H:%M'}} {self.title}"
        if self.participants:
            text += f" ({', '.join(self.participants)})"
        return f"{text}, {self.location}" if self.location else text


async def active_events(conn: AsyncConnection, household_id: str, *, recurring: bool | None = None,
                        member_id: str | None = None) -> list[dict[str, Any]]:
    return await fetch_all(
        conn,
        _EVENT + """ where e.household_id = :h and e.status = 'active'
                       and (cast(:recurring as boolean) is null or (e.rrule is not null) = :recurring)
                       and (cast(:member as uuid) is null or cast(:member as uuid) = any(e.participant_ids))
                     order by e.starts_at""",
        h=household_id, recurring=recurring, member=member_id,
    )


async def occurrences_between(conn: AsyncConnection, household_id: str, start: datetime, end: datetime,
                              member_id: str | None = None) -> list[Occurrence]:
    """Every event occurrence with start <= t < end, recurring ones expanded, soonest first."""
    found: list[Occurrence] = []
    for event in await active_events(conn, household_id, member_id=member_id):
        if event["rrule"]:
            starts = occurrences(event["rrule"], event["starts_at"], event["timezone"], start, end, event["exdates"])
        else:
            starts = [event["starts_at"]] if start <= event["starts_at"] < end else []
        found += [
            Occurrence(event["id"], event["title"], at, event["location"], event["participants"], event["rrule"])
            for at in starts
        ]
    return sorted(found, key=lambda occurrence: occurrence.start)


async def standalone_reminders(conn: AsyncConnection, household_id: str, start: datetime, end: datetime,
                               member_id: str | None = None, *, expand: bool = True) -> list[dict[str, Any]]:
    """Scheduled reminders that belong to no event and fire in the window. A repeating one
    appears once per occurrence, or only at its next time when `expand` is false."""
    rows = await fetch_all(
        conn,
        """select r.id, r.text, r.fire_at, r.rrule, r.target, m.name as member, h.timezone
           from reminders r join households h on h.id = r.household_id left join members m on m.id = r.member_id
           where r.household_id = :h and r.event_id is null and r.status = 'scheduled' and r.fire_at < :end
             and (cast(:member as uuid) is null or r.target = 'household' or r.member_id = :member)""",
        h=household_id, end=end, member=member_id,
    )
    found: list[dict[str, Any]] = []
    for row in rows:
        if row["rrule"]:
            times = occurrences(row["rrule"], row["fire_at"], row["timezone"], max(start, row["fire_at"]), end)
            times = times if expand else times[:1]
        else:
            times = [row["fire_at"]] if row["fire_at"] >= start else []
        found += [{**row, "fire_at": at} for at in times]
    return sorted(found, key=lambda row: row["fire_at"])


async def upcoming_lines(conn: AsyncConnection, household_id: str, timezone: str, start: datetime, end: datetime,
                         member_id: str | None = None) -> list[str]:
    """Events and standalone reminders in the window as one time-ordered list of lines."""
    entries = [(o.start, o.line(timezone) + (" [repeats]" if o.rrule else ""))
               for o in await occurrences_between(conn, household_id, start, end, member_id)]
    for row in await standalone_reminders(conn, household_id, start, end, member_id):
        who = row["member"] or "everyone"
        entries.append((row["fire_at"], f"{local(row['fire_at'], timezone):%a %-d %b %H:%M} reminder: "
                                        f"{row['text']} ({who})" + (" [repeats]" if row["rrule"] else "")))
    return [line for _, line in sorted(entries)]


async def brief_only(conn: AsyncConnection, household_id: str, start: datetime, end: datetime) -> list[Occurrence]:
    """Occurrences in the window whose day-before reminder fell in quiet hours and was skipped,
    so the morning brief is where the household hears about them (spec 9.2)."""
    found = []
    events = {event["id"]: event for event in await active_events(conn, household_id)}
    for occurrence in await occurrences_between(conn, household_id, start, end):
        event = events[occurrence.event_id]
        _, member_id = await _target(conn, event)
        day_before = occurrence.start - timedelta(minutes=DAY_MINUTES)
        if DAY_MINUTES in event["remind_before_minutes"] and await members.quiet_until(
                conn, household_id, member_id, day_before) is not None:
            found.append(occurrence)
    return found
