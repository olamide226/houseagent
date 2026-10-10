"""Calendar tools: schedule_event, modify_event, list_upcoming, set_reminder, add_dates."""
import calendar as weekdays
import re
from datetime import date, datetime, time, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.actions import record
from app.agent.base import Ctx, ToolError
from app.agent.resolve import EVENT_THRESHOLD, rank_events, resolve_members
from app.core.timeutil import local, to_utc
from app.services import calendar, households

LOCAL_TIME = "Household local time as ISO 8601 without an offset, e.g. 2026-10-07T10:30"


def _unknown(names: list[str]) -> list[str]:
    return [f"NOTE: nobody called '{name}' is in the family yet, so they are not linked" for name in names]


class ScheduleEvent(BaseModel):
    title: str
    kind: Literal["appointment", "activity", "task"] = "appointment"
    starts_at: datetime = Field(description=LOCAL_TIME)
    ends_at: datetime | None = None
    rrule: str | None = Field(None, description="RFC 5545, e.g. FREQ=WEEKLY;BYDAY=TU,TH")
    participants: list[str] = Field([], description="Names, 'me', 'us', or 'the kids'")
    location: str | None = None
    remind_before_minutes: list[int] = Field([1440, 60], description="Default: day before and 1h before")
    notes: str | None = None


async def schedule_event(ctx: Ctx, args: ScheduleEvent) -> str:
    """Create an appointment or recurring activity, with reminders. Appears in the ICS feed."""
    timezone = await households.timezone(ctx.conn, ctx.household_id)
    participants, unknown = await resolve_members(ctx.conn, ctx.household_id, args.participants, ctx.member_id)
    async with record(ctx, "schedule_event", args) as rec:
        await calendar.schedule_event(
            rec, title=args.title, kind=args.kind, starts_at=to_utc(args.starts_at, timezone),
            ends_at=args.ends_at and to_utc(args.ends_at, timezone), rrule=args.rrule,
            participant_ids=participants, location=args.location,
            remind_before_minutes=args.remind_before_minutes, notes=args.notes,
        )
        rec.lines += _unknown(unknown)
    return rec.result


class ModifyEvent(BaseModel):
    event: str = Field(description="How the user refers to it, e.g. 'Ada's GP appointment'")
    cancel: bool = False
    scope: Literal["this", "all"] = Field("this", description="For recurring events")
    occurrence: date | None = Field(None, description="With scope 'this': the date to change. Default: the next one")
    starts_at: datetime | None = Field(None, description=LOCAL_TIME)
    ends_at: datetime | None = None
    title: str | None = None
    location: str | None = None
    participants: list[str] | None = None
    notes: str | None = None


async def modify_event(ctx: Ctx, args: ModifyEvent) -> str:
    """Move, edit or cancel an event. Reminders are regenerated automatically."""
    timezone = await households.timezone(ctx.conn, ctx.household_id)
    ranked = await rank_events(ctx.conn, ctx.household_id, args.event, ctx.now)
    if not ranked or ranked[0].score < EVENT_THRESHOLD:
        coming = ", ".join(f"{c.title} ({local(c.next_start, timezone):%a %-d %b})" for c in ranked[:8])
        raise ToolError(f"no upcoming event matches '{args.event}'. Upcoming: {coming or 'nothing'}")
    participants = unknown = None
    if args.participants is not None:
        participants, unknown = await resolve_members(ctx.conn, ctx.household_id, args.participants, ctx.member_id)
    async with record(ctx, "modify_event", args) as rec:
        await calendar.modify_event(
            rec, ranked[0].id, cancel=args.cancel, scope=args.scope, occurrence=args.occurrence,
            starts_at=args.starts_at and to_utc(args.starts_at, timezone),
            ends_at=args.ends_at and to_utc(args.ends_at, timezone),
            title=args.title, location=args.location, participant_ids=participants, notes=args.notes,
        )
        rec.lines += _unknown(unknown or [])
    return rec.result


class ListUpcoming(BaseModel):
    days: int = Field(7, ge=1, le=60)
    member: str | None = None


async def list_upcoming(ctx: Ctx, args: ListUpcoming) -> str:
    """What's coming up, optionally for one person. Includes standalone reminders."""
    member_id = None
    if args.member:
        found, _ = await resolve_members(ctx.conn, ctx.household_id, [args.member], ctx.member_id)
        if len(found) != 1:
            raise ToolError(f"nobody called '{args.member}' is in the family")
        member_id = found[0]
    timezone = await households.timezone(ctx.conn, ctx.household_id)
    lines = await calendar.upcoming_lines(ctx.conn, ctx.household_id, timezone, ctx.now,
                                          ctx.now + timedelta(days=args.days), member_id)
    return "\n".join(lines) or f"Nothing in the next {args.days} days."


class SetReminder(BaseModel):
    text: str
    fire_at: datetime | None = Field(None, description=f"One-off time. {LOCAL_TIME}")
    rrule: str | None = Field(None, description="Repeating, e.g. FREQ=WEEKLY;BYDAY=SU;BYHOUR=18")
    target: str = Field("me", description="'me', 'household', or a member name")
    urgent: bool = Field(False, description="True only if the user wants it even during quiet hours")


async def set_reminder(ctx: Ctx, args: SetReminder) -> str:
    """A reminder not tied to an event ('remind me to call the landlord Friday at 9'). Whatever they
    ask to be reminded of is this, once or repeating, a chore included ('remind us every Sunday at
    6pm to put the bins out'). When someone answers a reminder you sent with 'later', set it again
    with the same words: two hours on, or at the time they say."""
    timezone = await households.timezone(ctx.conn, ctx.household_id)
    member_id = None
    if args.target.strip().lower() not in ("household", "everyone", "us", "we", "family", "the family"):
        found, _ = await resolve_members(ctx.conn, ctx.household_id, [args.target], ctx.member_id)
        if len(found) != 1:
            raise ToolError(f"nobody called '{args.target}' is in the family; use 'me', 'household' or a name")
        member_id = found[0]
    async with record(ctx, "set_reminder", args) as rec:
        await calendar.set_reminder(
            rec, text=args.text, fire_at=args.fire_at and to_utc(args.fire_at, timezone), rrule=args.rrule,
            member_id=member_id, urgent=args.urgent,
        )
    return rec.result


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class DatedEntry(BaseModel):
    title: str = Field(description="What it is, in a few words: 'School trip', 'Zara's party', 'Return the trip slip'")
    day: date = Field(description=(
        "The day it is on, or its first day, e.g. 2026-10-15. The date as the text gives it: never moved to fit "
        "the weekday"))
    weekday: str | None = Field(None, description=(
        "The weekday the text itself gives for that day, if it gives one. Leave empty once the person has told "
        "you which day is right"))
    start_time: time | None = Field(None, description=(
        "Household local time, e.g. 08:45. Only if the text gives one: never guess a time"))
    end_time: time | None = None
    last_day: date | None = Field(None, description="Only if it runs over several days")
    participants: list[str] = Field([], description=(
        "The family members it is for: names, 'me', 'us' or 'the kids'. Not other people the text names"))
    location: str | None = None
    notes: str | None = Field(None, description="What to bring, wear or pay, in a few words")
    rrule: str | None = Field(None, description="Only if it repeats and has a time, e.g. FREQ=WEEKLY;UNTIL=20261216")
    unsure: str | None = Field(None, description=(
        "Leave empty unless the text leaves this entry's day unclear. Then say what is unclear: the entry is held "
        "back so you can ask"))


class AddDates(BaseModel):
    entries: list[DatedEntry] = Field(min_length=1, max_length=50)   # a year of term dates is one letter


def _said(entry: DatedEntry) -> str:
    """An entry that was not added, with everything a later turn needs to add it: the photo or the
    file it came from is not shown to the model again."""
    when = f"{entry.day:%a %-d %b %Y}" + (f" {entry.start_time:%H:%M}" if entry.start_time else "")
    when += f" to {entry.end_time:%H:%M}" if entry.end_time else ""
    when += f", until {entry.last_day:%a %-d %b}" if entry.last_day else ""
    who = [f"for {' and '.join(entry.participants)}"] if entry.participants else []
    rest = [text.strip() for text in (entry.location, entry.notes) if text and text.strip()]
    return f"{entry.title.strip()} ({', '.join([when, *who, *rest])})"


# "... is a Monday", "... falls on a Monday": a weekday the model worked out for a date.
_CLAIMED_WEEKDAY = re.compile(r"\b(?:is|falls on)\s+(?:actually\s+)?an?\s+(mon|tue|wed|thu|fri|sat|sun)")


def _weekdays_said(text: str, day: date) -> list[str]:
    """The weekdays the turn's own words give for this date, however it is written: "Wednesday 12
    November", "Wed, Nov 12th", "Weds 12/11". A model does not always pass the weekday on; what
    arrived as words (a forward, pasted text, a PDF) can be read here without it."""
    weekday = r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+(?:the\s+)?"
    month, ordinal = f"{day:%b}".lower(), r"(?:st|nd|rd|th)?"
    written = (rf"{day.day}{ordinal}\s+(?:of\s+)?{month}", rf"{month}[a-z]*\.?\s+{day.day}{ordinal}\b",
               rf"0?{day.day}[/.]0?{day.month}\b(?!\s*[ap]\.?m)")   # "Wed 3.10 pm" is a time, not 3 October
    return [weekdays.day_name[WEEKDAYS.index(found[1])]
            for form in written for found in re.finditer(weekday + form, text.lower())]


def _doubt(entry: DatedEntry, said: str) -> tuple[str | None, str | None]:
    """Why an entry's day cannot be taken as read, and a note when a doubt was overruled.

    The weekday the text gives must be the weekday of its date. The turn's own words are believed
    first: where they give a weekday for that date, the one the model passed on is not asked,
    since a model that has quietly put the weekday right would pass on the right one.
    Otherwise the model's own doubt stands, except one that code can check: a model that says
    "10 December is a Monday" when it is the Thursday the text says has only got its sums wrong."""
    actual, passed_on = f"{entry.day:%a}".lower(), (entry.weekday or "").strip()
    named = _weekdays_said(said, entry.day) or ([passed_on] if passed_on.lower()[:3] in WEEKDAYS else [])
    if named and actual not in [name.lower()[:3] for name in named]:
        return f"the text says {named[0]}, but {entry.day:%-d %b %Y} is a {entry.day:%A}", None
    unsure = (entry.unsure or "").strip().rstrip(".")
    claimed = _CLAIMED_WEEKDAY.findall(unsure.lower())
    if named and claimed and actual not in claimed:
        return None, f"NOTE: {entry.day:%-d %b %Y} is a {entry.day:%A}, as the text says, so it was added"
    return unsure or None, None


def _passed(entry: DatedEntry, now: datetime, timezone: str) -> bool:
    if entry.rrule:
        return False
    if entry.start_time is None:
        return (entry.last_day or entry.day) < local(now, timezone).date()
    return to_utc(datetime.combine(entry.day, entry.start_time), timezone) <= now


async def add_dates(ctx: Ctx, args: AddDates) -> str:
    """Put everything dated in a letter, an invite, an appointment message or a newsletter on the
    calendar: one call, one entry per dated thing. For what someone sent as a photo, a file, a
    forward or pasted text; what a person simply tells you goes through schedule_event or
    set_reminder. One undo takes the whole call back.
    An entry with a time becomes a calendar entry with the usual reminders. An entry with a day and
    no time (a day off school, a form due back) becomes one reminder the evening before.
    An entry is held back when you say you are unsure of it or when the weekday and the date in the
    text disagree (AMBIGUOUS:); the other entries are still added. Then ask one short question
    about what was held back. Nothing is on the calendar until this call has answered.
    Reply with one short list: each thing added with its day and time, then what was left out and
    why. Say that reminders are set in one closing line, not their times for each entry."""
    timezone = await households.timezone(ctx.conn, ctx.household_id)
    added: list[str] = []
    async with record(ctx, "add_dates", args) as rec:
        for entry in args.entries:
            doubt, overruled = _doubt(entry, ctx.said)
            if doubt:
                rec.lines.append(f"AMBIGUOUS: {_said(entry)} not added: {doubt}")
                continue
            if _passed(entry, ctx.now, timezone):
                rec.lines.append(f"NOTE: {_said(entry)} left out: it has already passed")
                continue
            participants, unknown = await resolve_members(ctx.conn, ctx.household_id, entry.participants,
                                                          ctx.member_id)
            written = len(rec.touched)
            try:
                async with ctx.conn.begin_nested():
                    if entry.start_time is None:
                        if entry.rrule:
                            raise ToolError("something that repeats needs a time")
                        await calendar.remind_of_day(
                            rec, title=entry.title, day=entry.day, last_day=entry.last_day,
                            participant_ids=participants, location=entry.location, notes=entry.notes)
                    else:
                        ends = (datetime.combine(entry.last_day or entry.day, entry.end_time)
                                if entry.end_time else None)
                        await calendar.schedule_event(
                            rec, title=entry.title, kind="activity" if entry.rrule else "appointment",
                            starts_at=to_utc(datetime.combine(entry.day, entry.start_time), timezone),
                            ends_at=ends and to_utc(ends, timezone), rrule=entry.rrule,
                            participant_ids=participants, location=entry.location, notes=entry.notes)
            except ToolError as exc:
                rec.lines.append(f"ERROR: {_said(entry)} not added: {exc}")
                continue
            rec.lines += [*_unknown(unknown), *([overruled] if overruled else [])]
            if len(rec.touched) > written:
                added.append(entry.title.strip())
        if len(added) > 1:
            rec.lines.insert(0, f"OK: {len(added)} added from one message, and one undo takes them all back: "
                                + ", ".join(added))
    return rec.result
