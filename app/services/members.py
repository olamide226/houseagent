"""The family: members, invites, quiet hours and dashboard logins. Shared by agent tools and
the dashboard. Tokens are stored as SHA-256 hashes only."""
import secrets
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.agent.actions import Recorder
from app.agent.base import ToolError
from app.core.identity import INVITE_TTL, hash_token, new_invite_code
from app.core.timeutil import quiet_end
from app.db import execute, fetch_all, fetch_one, fetch_val, jsonb

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


async def family(conn: AsyncConnection, household_id: str, now: datetime) -> list[dict[str, Any]]:
    """Everyone in the household with their connected channels and whether an invite is open."""
    return await fetch_all(
        conn,
        """select m.id, m.name, m.role, m.is_admin, m.preferred_channel, m.quiet_start, m.quiet_end,
                  m.presence_token_hash is not null as has_presence_link,
                  case when m.invite_expires_at > :now then m.invite_expires_at end as invite_open_until,
                  coalesce((select array_agg(ci.channel order by ci.channel) from channel_identities ci
                            where ci.member_id = m.id), '{}') as channels
           from members m where m.household_id = :h order by m.role, m.created_at""",
        h=household_id, now=now,
    )


async def identities(conn: AsyncConnection, household_id: str) -> list[dict[str, Any]]:
    """Every connected handle in the household, with whose it is and when it connected."""
    return await fetch_all(
        conn,
        """select ci.channel, ci.handle, ci.verified_at, m.id as member_id, m.name
           from channel_identities ci join members m on m.id = ci.member_id where m.household_id = :h""",
        h=household_id,
    )


async def _member(rec: Recorder, member_id: str, *, adult: bool = False) -> dict[str, Any]:
    member = await fetch_one(
        rec.ctx.conn, "select id, name, role from members where id = :id and household_id = :h for update",
        id=member_id, h=rec.ctx.household_id,
    )
    if member is None:
        raise ToolError("nobody like that is in the family")
    if adult and member["role"] != "adult":
        raise ToolError(f"{member['name']} is a child: only adults have a chat, quiet hours or a login")
    return member


async def add_member(rec: Recorder, name: str, role: str) -> tuple[dict[str, Any], bool]:
    """Add someone to the family. Returns (member, created); a name already there is not added twice."""
    name = " ".join(name.split())
    if not name or role not in ("adult", "child"):
        raise ToolError("give a name, and adult or child")
    existing = await fetch_one(
        rec.ctx.conn, "select id, name, role from members where household_id = :h and lower(name) = lower(:name)",
        h=rec.ctx.household_id, name=name,
    )
    if existing:
        rec.lines.append(f"OK: {existing['name']} is already in the family ({existing['role']})")
        return existing, False
    member_id = str(await fetch_val(
        rec.ctx.conn, "insert into members (household_id, name, role) values (:h, :name, :role) returning id",
        h=rec.ctx.household_id, name=name, role=role,
    ))
    rec.created("members", member_id)
    rec.lines.append(f"NEW: {name} ({role})")
    return {"id": member_id, "name": name, "role": role}, True


async def name_of(conn: AsyncConnection, member_id: str) -> str:
    return str(await fetch_val(conn, "select name from members where id = :id", id=member_id))


async def is_connected(conn: AsyncConnection, member_id: str) -> bool:
    return bool(await fetch_val(conn, "select exists (select 1 from channel_identities where member_id = :m)",
                                m=member_id))


async def invite(rec: Recorder, member_id: str) -> str:
    """A fresh invite code for an adult; any earlier code stops working. Shown once: only its hash is kept."""
    member = await _member(rec, member_id, adult=True)
    code = await create_invite(rec.ctx.conn, member_id, rec.ctx.now)
    rec.appended("members", member_id)
    rec.lines.append(f"OK: new invite for {member['name']}, valid 7 days")
    return code


async def revoke_invite(rec: Recorder, member_id: str) -> None:
    member = await _member(rec, member_id)
    await execute(rec.ctx.conn, "update members set invite_code_hash = null, invite_expires_at = null "
                                "where id = :id", id=member_id)
    rec.appended("members", member_id)
    rec.lines.append(f"OK: invite for {member['name']} revoked")


async def new_presence_token(conn: AsyncConnection, member_id: str) -> str:
    """A fresh presence token (32 random bytes, URL-safe), replacing any earlier one. Only its
    hash is stored, so the link is shown or sent once."""
    token = secrets.token_urlsafe(32)
    await execute(conn, "update members set presence_token_hash = :hash where id = :id",
                  hash=hash_token(token), id=member_id)
    return token


async def presence_link(rec: Recorder, member_id: str) -> str:
    """A new presence token for an adult, from the dashboard; their earlier link stops working."""
    member = await _member(rec, member_id, adult=True)
    token = await new_presence_token(rec.ctx.conn, member_id)
    rec.appended("members", member_id)
    rec.lines.append(f"OK: new shop-arrival link for {member['name']}; any earlier link has stopped working")
    return token


async def for_presence_token(conn: AsyncConnection, token: str) -> dict[str, Any] | None:
    return await fetch_one(
        conn,
        """select m.id, m.household_id, m.name, h.timezone from members m join households h on h.id = m.household_id
           where m.presence_token_hash = :hash and m.role = 'adult'""",
        hash=hash_token(token),
    )


async def record_connection(conn: AsyncConnection, member: dict[str, Any], channel: str) -> None:
    """Log an invite redemption, so it shows in Activity and adding the member can no longer be undone."""
    await execute(
        conn,
        """insert into agent_actions (household_id, member_id, tool, args, result, touched, created_at)
           values (:h, :member, 'invite.redeem', cast(:args as jsonb), :result, cast(:touched as jsonb),
                   clock_timestamp())""",
        h=member["household_id"], member=member["id"], args=jsonb({"channel": channel}),
        result=f"OK: {member['name']} connected on {channel}",
        touched=jsonb([{"table": "members", "id": member["id"]}]),
    )


async def set_quiet_hours(rec: Recorder, member_ids: list[str], start: time | None, end: time | None) -> None:
    """When each adult is not to be disturbed; None for both turns quiet hours off."""
    if (start is None) != (end is None) or (start is not None and start == end):
        raise ToolError("quiet hours need a start and a different end")
    names = []
    for member_id in member_ids:
        names.append((await _member(rec, member_id, adult=True))["name"])
        await rec.before("members", id=member_id)
        await execute(rec.ctx.conn, "update members set quiet_start = :start, quiet_end = :end where id = :id",
                      start=start, end=end, id=member_id)
    if not names:
        raise ToolError("nobody to set quiet hours for")
    span = f"{start:%H:%M} to {end:%H:%M}" if start and end else "off"
    rec.lines.append(f"OK: quiet hours for {', '.join(names)}: {span}")


async def set_preferred_channel(rec: Recorder, member_id: str, channel: str) -> None:
    """Where a member's DMs go. Only a channel they have connected."""
    member = await _member(rec, member_id, adult=True)
    if not await fetch_val(rec.ctx.conn, "select exists (select 1 from channel_identities "
                                         "where member_id = :m and channel = :c)", m=member_id, c=channel):
        raise ToolError(f"{member['name']} has not connected {channel}")
    await rec.before("members", id=member_id)
    await execute(rec.ctx.conn, "update members set preferred_channel = :c where id = :id", c=channel, id=member_id)
    rec.lines.append(f"OK: {member['name']} is messaged on {channel}")


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


async def quiet_until(conn: AsyncConnection, household_id: str, member_id: str | None,
                      moment: datetime) -> datetime | None:
    """When the quiet hours holding a send at `moment` end, or None if it can go now.

    A send to one member is held by that member's quiet hours; a send to the household is
    held while any adult is in theirs, until the last of them is out."""
    rows = await fetch_all(
        conn,
        """select m.quiet_start, m.quiet_end, h.timezone from members m join households h on h.id = m.household_id
           where m.household_id = :h and m.role = 'adult' and (cast(:member as uuid) is null or m.id = :member)""",
        h=household_id, member=member_id,
    )
    ends = [quiet_end(moment, row["timezone"], row["quiet_start"], row["quiet_end"]) for row in rows]
    return max((end for end in ends if end is not None), default=None)
