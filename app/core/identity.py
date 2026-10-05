"""Handle -> member lookup and invite codes (spec sections 7.1 and 12.1)."""
import hashlib
import re
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.envelope import Channel
from app.db import execute, fetch_one

INVITE_RE = re.compile(r"^[A-Z]{4}-[A-Z0-9]{4}$")
INVITE_TTL = timedelta(days=7)
# No 0/O/1/I, so a code read aloud or off a screen cannot be mistyped.
_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_ALNUM = _LETTERS + "23456789"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_invite_code() -> str:
    head = "".join(secrets.choice(_LETTERS) for _ in range(4))
    tail = "".join(secrets.choice(_ALNUM) for _ in range(4))
    return f"{head}-{tail}"


def parse_invite_code(text: str | None) -> str | None:
    """The code if the whole message is one (case-insensitive), else None."""
    code = (text or "").strip().upper()
    return code if INVITE_RE.fullmatch(code) else None


async def member_for_handle(conn: AsyncConnection, channel: Channel, handle: str) -> dict[str, Any] | None:
    return await fetch_one(
        conn,
        """select m.id, m.household_id, m.name, m.role, m.is_admin
           from channel_identities ci join members m on m.id = ci.member_id
           where ci.channel = :channel and ci.handle = :handle""",
        channel=channel.value, handle=handle,
    )


async def redeem_invite(
    conn: AsyncConnection, channel: Channel, handle: str, code: str, now: datetime
) -> dict[str, Any] | None:
    """Link `handle` to the member holding `code`. A code works once per channel."""
    member = await fetch_one(
        conn,
        """select m.id, m.household_id, m.name from members m
           where m.invite_code_hash = :hash and m.invite_expires_at > :now
             and not exists (select 1 from channel_identities ci
                             where ci.member_id = m.id and ci.channel = :channel)
           for update""",
        hash=hash_token(code), now=now, channel=channel.value,
    )
    if member is None:
        return None
    linked = await execute(
        conn,
        """insert into channel_identities (member_id, channel, handle, verified_at)
           values (:member, :channel, :handle, :now) on conflict do nothing""",
        member=member["id"], channel=channel.value, handle=handle, now=now,
    )
    if not linked:
        return None
    await execute(
        conn,
        "update members set preferred_channel = coalesce(preferred_channel, :channel) where id = :member",
        member=member["id"], channel=channel.value,
    )
    return member
