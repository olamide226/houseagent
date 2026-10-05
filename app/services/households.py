"""Household bootstrap (spec section 12.1)."""
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import fetch_val
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
