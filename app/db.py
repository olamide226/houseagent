"""Engine, transactions and small query helpers. SQLAlchemy async Core, no ORM."""
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.config import get_settings

_engine: AsyncEngine | None = None


def engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
    return _engine


@asynccontextmanager
async def tx() -> AsyncIterator[AsyncConnection]:
    """One transaction: commits on exit, rolls back on error."""
    async with engine().begin() as conn:
        yield conn


async def advisory_lock(conn: AsyncConnection, household_id: str) -> None:
    """Serialise work per household until the surrounding transaction ends."""
    await conn.execute(text("select pg_advisory_xact_lock(hashtext(:h))"), {"h": household_id})


def _plain(value: Any) -> Any:
    return str(value) if isinstance(value, UUID) else value


async def fetch_all(conn: AsyncConnection, sql: str, **params: Any) -> list[dict[str, Any]]:
    result = await conn.execute(text(sql), params)
    return [{k: _plain(v) for k, v in row.items()} for row in result.mappings()]


async def fetch_one(conn: AsyncConnection, sql: str, **params: Any) -> dict[str, Any] | None:
    rows = await fetch_all(conn, sql, **params)
    return rows[0] if rows else None


async def fetch_val(conn: AsyncConnection, sql: str, **params: Any) -> Any:
    return _plain((await conn.execute(text(sql), params)).scalar())


async def execute(conn: AsyncConnection, sql: str, **params: Any) -> int:
    return (await conn.execute(text(sql), params)).rowcount


def jsonb(value: Any) -> str:
    """Serialise a value for a `cast(:param as jsonb)` bind."""
    return json.dumps(value, default=str)
