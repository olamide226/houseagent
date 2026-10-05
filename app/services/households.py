"""Household bootstrap (spec section 12.1), onboarding state, the brief time, facts, shops
and the calendar feed token."""
import secrets
from datetime import time
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncConnection

from app.agent.actions import Recorder
from app.agent.base import ToolError
from app.core.identity import hash_token
from app.db import execute, fetch_all, fetch_one, fetch_val, jsonb
from app.services import inventory

# Spec section 12.2, in order. `presence` joins the list with the Shortcut endpoint (milestone 5).
ONBOARDING_STEPS = ("family", "routines", "shops", "staples", "tour", "rhythm")


async def household_exists(conn: AsyncConnection) -> bool:
    return bool(await fetch_val(conn, "select exists (select 1 from households)"))


async def create_household(conn: AsyncConnection, name: str, timezone: str, admin_name: str) -> tuple[str, str]:
    """Create the household, its admin member and the seed locations. Returns (household, admin) ids."""
    ZoneInfo(timezone)  # raises on an unknown zone before anything is written
    household_id = str(await fetch_val(
        conn, "insert into households (name, timezone) values (:name, :tz) returning id", name=name, tz=timezone
    ))
    member_id = str(await fetch_val(
        conn,
        "insert into members (household_id, name, role, is_admin) values (:h, :name, 'adult', true) returning id",
        h=household_id, name=admin_name,
    ))
    for location, aliases in inventory.DEFAULT_LOCATIONS.items():
        await inventory.create_location(conn, household_id, location, aliases)
    return household_id, member_id


async def timezone(conn: AsyncConnection, household_id: str) -> str:
    return str(await fetch_val(conn, "select timezone from households where id = :h", h=household_id))


async def new_calendar_token(conn: AsyncConnection, household_id: str) -> str:
    """A fresh ICS feed token, replacing any earlier one. Only its hash is stored, so it is shown once."""
    token = secrets.token_urlsafe(32)
    await execute(conn, "update households set calendar_token_hash = :hash where id = :h",
                  hash=hash_token(token), h=household_id)
    return token


async def for_calendar_token(conn: AsyncConnection, token: str) -> dict[str, Any] | None:
    return await fetch_one(conn, "select id, name, timezone from households where calendar_token_hash = :hash",
                           hash=hash_token(token))


# ---------------------------------------------------------------- onboarding
async def onboarding(conn: AsyncConnection, household_id: str) -> dict[str, Any]:
    """`{"step": current or None, "remaining": [...]}`; step is None once setup is complete."""
    state = await fetch_val(conn, "select onboarding_state from households where id = :h", h=household_id)
    remaining = [step for step in ONBOARDING_STEPS if step not in state.get("done", [])]
    if state.get("step") is None:
        remaining = []
    return {"step": remaining[0] if remaining else None, "remaining": remaining}


async def advance_onboarding(rec: Recorder, step: str, skipped: bool = False) -> None:
    """Mark a step done or skipped and move to the first step still open."""
    if step not in ONBOARDING_STEPS:
        raise ToolError(f"no step called {step}; the steps are {', '.join(ONBOARDING_STEPS)}")
    conn, household_id = rec.ctx.conn, rec.ctx.household_id
    state = await fetch_val(conn, "select onboarding_state from households where id = :h for update",
                            h=household_id)
    if state.get("step") is None:
        raise ToolError("setup is already complete")
    done = [*state.get("done", []), *([] if step in state.get("done", []) else [step])]
    was_skipped = [*state.get("skipped", []), *([step] if skipped else [])]
    remaining = [name for name in ONBOARDING_STEPS if name not in done]
    await rec.before("households", id=household_id)
    await execute(
        conn, "update households set onboarding_state = cast(:state as jsonb) where id = :h", h=household_id,
        state=jsonb({"step": remaining[0] if remaining else None, "done": done, "skipped": was_skipped}),
    )
    rec.lines.append(f"OK: {step} {'skipped' if skipped else 'done'}. "
                     + (f"Next step: {remaining[0]}" if remaining else "Setup is complete"))


# ---------------------------------------------------------------- settings and facts
async def set_digest_time(rec: Recorder, at: time) -> None:
    await rec.before("households", id=rec.ctx.household_id)
    await execute(rec.ctx.conn, "update households set digest_time = :at where id = :h",
                  at=at, h=rec.ctx.household_id)
    rec.lines.append(f"OK: morning brief at {at:%H:%M}")


async def facts(conn: AsyncConnection, household_id: str) -> list[dict[str, Any]]:
    return await fetch_all(
        conn,
        """select f.id, f.key, f.value, m.name as member from household_facts f
           left join members m on m.id = f.member_id where f.household_id = :h order by m.name nulls first, f.key""",
        h=household_id,
    )


async def set_fact(rec: Recorder, key: str, value: str | None, member_id: str | None = None) -> None:
    """Upsert a fact about the household, or about one member; no value forgets it."""
    conn, household_id = rec.ctx.conn, rec.ctx.household_id
    value = (value or "").strip()
    if not key:
        raise ToolError("a fact needs a name")
    fact_id = await fetch_val(
        conn, "select id from household_facts where household_id = :h and key = :key "
              "and member_id is not distinct from :member", h=household_id, key=key, member=member_id)
    if fact_id is None and not value:
        rec.lines.append(f"OK: nothing was remembered as {key}")
        return
    if fact_id is None:
        fact_id = await fetch_val(
            conn, "insert into household_facts (household_id, member_id, key, value, updated_at) "
                  "values (:h, :member, :key, :value, clock_timestamp()) returning id",
            h=household_id, member=member_id, key=key, value=value)
        rec.created("household_facts", str(fact_id))
    else:
        await rec.before("household_facts", id=str(fact_id))
        if value:
            await execute(conn, "update household_facts set value = :value, updated_at = clock_timestamp() "
                                "where id = :id", value=value, id=fact_id)
        else:
            await execute(conn, "delete from household_facts where id = :id", id=fact_id)
    rec.lines.append(f"OK: {key} = {value}" if value else f"OK: forgot {key}")


async def add_stores(rec: Recorder, names: list[str]) -> None:
    """Shops the family uses, as `places` of kind store (matched by store-arrival nudges later)."""
    for name in names:
        place = await fetch_one(
            rec.ctx.conn, "select id, kind from places where household_id = :h and lower(name) = lower(:name)",
            h=rec.ctx.household_id, name=name)
        if place is None:
            place_id = await fetch_val(
                rec.ctx.conn, "insert into places (household_id, name, kind) values (:h, :name, 'store') "
                              "returning id", h=rec.ctx.household_id, name=name)
            rec.created("places", str(place_id))
            rec.lines.append(f"NEW: {name} (shop)")
        elif place["kind"] != "store":
            await rec.before("places", id=place["id"])
            await execute(rec.ctx.conn, "update places set kind = 'store' where id = :id", id=place["id"])
