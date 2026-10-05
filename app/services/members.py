"""Member invites and dashboard logins. Tokens are stored as SHA-256 hashes only."""
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.identity import INVITE_TTL, hash_token, new_invite_code
from app.db import execute, fetch_one, fetch_val

LOGIN_TTL = timedelta(minutes=10)
LOGINS_PER_HOUR = 5


async def create_invite(conn: AsyncConnection, member_id: str, now: datetime) -> str:
    """A fresh invite code for the member, replacing any earlier one. Valid 7 days."""
    code = new_invite_code()
    await execute(
        conn, "update members set invite_code_hash = :hash, invite_expires_at = :expires where id = :id",
        hash=hash_token(code), expires=now + INVITE_TTL, id=member_id,
    )
    return code


async def create_login_token(conn: AsyncConnection, member_id: str, now: datetime) -> str | None:
    """A one-time dashboard login token, or None for children and past 5 links an hour."""
    allowed = await fetch_val(
        conn,
        """select m.role = 'adult' and (select count(*) from login_tokens t
                                       where t.member_id = m.id and t.created_at > :since) < :limit
           from members m where m.id = :id""",
        id=member_id, since=now - timedelta(hours=1), limit=LOGINS_PER_HOUR,
    )
    if not allowed:
        return None
    token = secrets.token_urlsafe(32)
    await execute(
        conn,
        "insert into login_tokens (token_hash, member_id, expires_at, created_at) "
        "values (:hash, :member, :expires, :now)",
        hash=hash_token(token), member=member_id, expires=now + LOGIN_TTL, now=now,
    )
    return token


async def consume_login_token(conn: AsyncConnection, token: str, now: datetime) -> dict[str, Any] | None:
    """Exchange an unused, unexpired token for its member. Single use."""
    return await fetch_one(
        conn,
        """update login_tokens t set used_at = :now from members m
           where t.token_hash = :hash and t.used_at is null and t.expires_at > :now and m.id = t.member_id
           returning m.id, m.session_version""",
        hash=hash_token(token), now=now,
    )


async def session_member(conn: AsyncConnection, member_id: str, session_version: int) -> dict[str, Any] | None:
    return await fetch_one(
        conn,
        """select m.id, m.household_id, m.name, m.is_admin, h.name as household, h.timezone
           from members m join households h on h.id = m.household_id
           where m.id = :id and m.session_version = :version and m.role = 'adult'""",
        id=member_id, version=session_version,
    )


async def log_out_everywhere(conn: AsyncConnection, member_id: str) -> None:
    await execute(conn, "update members set session_version = session_version + 1 where id = :id", id=member_id)
