"""Memory tool: remember. Plain facts go to `household_facts`; a few keys are settings and
are written where the rest of the system reads them (ADR 0016)."""
import re
from datetime import time

from pydantic import BaseModel, Field

from app.agent.actions import Recorder, record
from app.agent.base import Ctx, ToolError
from app.agent.resolve import Ambiguous, resolve_item, resolve_members
from app.core.timeutil import parse_clock
from app.db import fetch_all
from app.services import households, inventory, members

_SPLIT = re.compile(r"\s*[,;\n]\s*")
_SPAN = re.compile(r"\s*(?:-|–|—|\bto\b|\buntil\b)\s*")
_OFF = {"", "off", "none", "no"}


class Remember(BaseModel):
    key: str = Field(description=(
        "snake_case, e.g. 'milk_brand', 'main_supermarket'. These keys change how the household runs: "
        "'staples' (things always kept in the house, comma-separated), 'shops' (where the family shops, "
        "comma-separated), 'morning_brief' (HH:MM), 'quiet_hours' (HH:MM-HH:MM, or off)"))
    value: str | None = Field(None, description="None forgets the fact")
    about: str | None = Field(None, description="Member name, or omit for the whole household")


async def remember(ctx: Ctx, args: Remember) -> str:
    """Save or forget a durable household fact or preference."""
    key = re.sub(r"[^a-z0-9]+", "_", args.key.lower()).strip("_")
    value = (args.value or "").strip()
    async with record(ctx, "remember", args) as rec:
        if key == "staples":
            await _staples(rec, _names(value))
        elif key == "morning_brief":
            await households.set_digest_time(rec, _clock(value))
        elif key == "quiet_hours":
            await _quiet_hours(rec, value, args.about)
        else:
            await households.set_fact(rec, key, value, await _one_member(ctx, args.about))
            if key in ("shops", "main_supermarket"):
                await households.add_stores(rec, _names(value))
    return rec.result


def _names(value: str) -> list[str]:
    return [name for name in _SPLIT.split(value) if name]


def _clock(value: str) -> time:
    try:
        return parse_clock(value)
    except ValueError:
        raise ToolError(f"'{value}' is not a time of day; use HH:MM, e.g. 07:30") from None


async def _staples(rec: Recorder, names: list[str]) -> None:
    if not names:
        raise ToolError("name the items to always keep in")
    for name in names:
        item = await resolve_item(rec.ctx.conn, rec.ctx.household_id, name)
        if isinstance(item, Ambiguous):
            rec.lines.append(f"AMBIGUOUS: '{name}' could be {', '.join(item.options)}")
            continue
        if item.created:
            rec.lines.append(f"NEW: {item.name}")
        await inventory.mark_staple(rec, item.id)


async def _quiet_hours(rec: Recorder, value: str, about: str | None) -> None:
    ctx = rec.ctx
    if about:
        people, unknown = await resolve_members(ctx.conn, ctx.household_id, [about], ctx.member_id)
        if unknown:
            raise ToolError(f"nobody called {about} is in the family")
    else:
        people = [row["id"] for row in await fetch_all(
            ctx.conn, "select id from members where household_id = :h and role = 'adult'", h=ctx.household_id)]
    if value.lower() in _OFF:
        await members.set_quiet_hours(rec, people, None, None)
        return
    span = _SPAN.split(value)
    if len(span) != 2:
        raise ToolError("give quiet hours as start-end, e.g. 21:30-07:00")
    await members.set_quiet_hours(rec, people, _clock(span[0]), _clock(span[1]))


async def _one_member(ctx: Ctx, about: str | None) -> str | None:
    if not about:
        return None
    people, unknown = await resolve_members(ctx.conn, ctx.household_id, [about], ctx.member_id)
    if unknown:
        raise ToolError(f"nobody called {about} is in the family")
    return people[0] if len(people) == 1 else None   # "us" is the whole household
