"""Calendar tools: schedule_event, modify_event, list_upcoming, set_reminder."""
from datetime import date, datetime, timedelta
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
    """A reminder not tied to an event ('remind me to call the landlord Friday at 9')."""
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
