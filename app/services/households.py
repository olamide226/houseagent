"""Household bootstrap (spec section 12.1) and the calendar feed token."""
import secrets
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.identity import hash_token
from app.db import execute, fetch_one, fetch_val
from app.services import inventory


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
