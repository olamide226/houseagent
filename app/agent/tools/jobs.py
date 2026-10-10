"""Household job tools: add_job, update_job."""
from datetime import date, datetime, time
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.actions import record
from app.agent.base import Ctx, ToolError
from app.agent.resolve import resolve_members
from app.agent.tools.calendar import LOCAL_TIME
from app.core.timeutil import to_utc
from app.services import households, jobs

EVERYONE = ("everyone", "household", "us", "we", "family", "the family", "anyone", "whoever", "all of us")
TIME_OF_DAY = ("Time of day as HH:MM, only if a time or a part of the day was said: morning 08:00, afternoon 15:00, "
               "evening or night 19:00. Leave it out for a day with no time")
REPEAT = "If it comes round again, RFC 5545: FREQ=WEEKLY;BYDAY=TU, FREQ=DAILY;INTERVAL=28, FREQ=YEARLY"


async def _owner(ctx: Ctx, name: str) -> str | None:
    """Whose job it is: a member's id, or None for everyone."""
    if name.strip().lower() in EVERYONE:
        return None
    found, _ = await resolve_members(ctx.conn, ctx.household_id, [name], ctx.member_id)
    if len(found) != 1:
        raise ToolError(f"nobody called '{name}' is in the family; use 'me', 'everyone' or a name")
    return found[0]


class AddJob(BaseModel):
    title: str = Field(description="What it is, in a few words: 'Put the bins out', 'MOT', \"Ada's prescription\", "
                                   "\"Grandma's birthday\"")
    due_on: date = Field(description="The day it is next due, YYYY-MM-DD. If it repeats, the first one from today on")
    at_time: time | None = Field(None, description=TIME_OF_DAY)
    rrule: str | None = Field(None, description=REPEAT)
    owner: str = Field("me", description=(
        "Whose job it is: 'me', a name, or 'everyone'. Something named as one of the family's (\"Ada's "
        "prescription\") is theirs unless they said who does it"))
    kind: Literal["job", "date"] = Field("job", description="'date' for a birthday or an anniversary: it comes "
                                                            "round every year and is only said, a week before and "
                                                            "on the day")
    remind_days_before: int | None = Field(None, ge=0, le=jobs.MAX_LEAD_DAYS,
                                           description="Only if they said how long before to be told")


async def add_job(ctx: Ctx, args: AddJob) -> str:
    """A household job, a renewal or a date they tell you about: whose it is, when it falls due or
    how often it comes round. 'The bins go out every Tuesday night, Ola does them', 'the MOT is due
    on 14 March', 'Ada's prescription every 28 days', a birthday. The owner is told when it is due
    and asked once more if nobody says it is done. Only when they describe the job itself: anything
    they ask to be reminded of ('remind me ...', 'remind us every Sunday to put the bins out') is
    set_reminder, and an appointment or a class is schedule_event. To change a job that exists,
    use update_job."""
    owner = await _owner(ctx, args.owner)
    async with record(ctx, "add_job", args) as rec:
        await jobs.add(rec, title=args.title, due_on=args.due_on, at_time=args.at_time, rrule=args.rrule,
                       member_id=owner, kind=args.kind, lead_days=args.remind_days_before)
    return rec.result


class UpdateJob(BaseModel):
    job: str | None = Field(None, description="How they refer to it, e.g. 'bins'. Leave it out when they are "
                                              "answering your own message about a job")
    action: Literal["done", "later", "skip", "change", "stop"] = Field(description=(
        "done: someone says it is done. later: ask again later. skip: not this time, the next one stands. "
        "change: a new owner, day, time, repeat or name. stop: no more of it"))
    later_until: datetime | None = Field(None, description=(
        f"With later, only if they named a time ('in an hour', 'tomorrow morning'). {LOCAL_TIME}. "
        "Leave it out for a bare 'later': the usual wait is applied for you"))
    title: str | None = Field(None, description="With change: a new name")
    owner: str | None = Field(None, description="With change: 'me', a name, or 'everyone'")
    due_on: date | None = Field(None, description="With change: the day it is next due, YYYY-MM-DD")
    at_time: time | None = Field(None, description=f"With change. {TIME_OF_DAY}")
    rrule: str | None = Field(None, description=f"With change. {REPEAT}")
    remind_days_before: int | None = Field(None, ge=0, le=jobs.MAX_LEAD_DAYS)


async def update_job(ctx: Ctx, args: UpdateJob) -> str:
    """Tick off, put off, skip, change or stop a household job from the Jobs line of the brief.
    Use done only when someone says they have done it. After done, reply with exactly ACK; after
    later, say when you will ask again."""
    job_id = await jobs.meant(ctx.conn, ctx.household_id, args.job, ctx.message_id)
    owner, everyone = None, False
    if args.action == "change" and args.owner:
        owner = await _owner(ctx, args.owner)
        everyone = owner is None
    async with record(ctx, "update_job", args) as rec:
        if args.action == "done":
            await jobs.done(rec, job_id)
        elif args.action == "later":
            timezone = await households.timezone(ctx.conn, ctx.household_id)
            await jobs.later(rec, job_id, args.later_until and to_utc(args.later_until, timezone))
        elif args.action == "skip":
            await jobs.skip(rec, job_id)
        elif args.action == "stop":
            await jobs.stop(rec, job_id)
        else:
            await jobs.change(rec, job_id, title=args.title, member_id=owner, everyone=everyone, due_on=args.due_on,
                              at_time=args.at_time, rrule=args.rrule, lead_days=args.remind_days_before)
    return rec.result
