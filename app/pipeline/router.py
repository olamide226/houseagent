"""Outbound router (spec section 7.3). Every send is an `outbox` row; this dispatches them.

Destinations are resolved only from the household's own `threads` and `channel_identities`
rows, so nothing can be addressed to anyone outside the family.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncConnection

from app.channels.base import ChannelAdapter, NotSupported
from app.core.envelope import Channel, OutboundMessage
from app.core.timeutil import utcnow
from app.db import execute, fetch_all, fetch_one, fetch_val, jsonb, tx

log = structlog.get_logger()

BACKOFF_SECONDS = [10, 30, 120, 600, 1800]
CHANNEL_ORDER = [Channel.telegram, Channel.whatsapp, Channel.imessage]


async def enqueue(conn: AsyncConnection, message: OutboundMessage, *, status: str = "pending") -> str | None:
    """Queue a send inside the caller's transaction. None when `dedupe_key` was already queued."""
    row_id = await fetch_val(
        conn,
        """insert into outbox (household_id, target, thread_id, member_id, text, react_emoji,
                               reply_to_message_id, urgency, respect_quiet_hours, dedupe_key, status,
                               created_at, send_after)
           values (:household_id, :target, :thread_id, :member_id, :text, :react_emoji,
                   :reply_to_message_id, :urgency, :respect_quiet_hours, :dedupe_key, :status,
                   clock_timestamp(), clock_timestamp())
           on conflict (household_id, dedupe_key) do nothing returning id""",
        **message.model_dump(), status=status,
    )
    return None if row_id is None else str(row_id)


async def retry(conn: AsyncConnection, household_id: str, outbox_id: str) -> bool:
    """Put a failed send back in the queue."""
    return bool(await execute(
        conn,
        """update outbox set status = 'pending', attempts = 0, send_after = clock_timestamp(), last_error = null
           where id = :id and household_id = :h and status = 'failed'""",
        id=outbox_id, h=household_id,
    ))


def split_text(text: str, limit: int) -> list[str]:
    """Split text longer than `limit` on paragraph boundaries, then lines, then hard."""
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = max(rest.rfind("\n\n", 0, limit), 0) or max(rest.rfind("\n", 0, limit), 0) or limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return [*chunks, rest] if rest else chunks


@dataclass(frozen=True)
class Destination:
    channel: Channel
    external_thread_id: str
    thread_id: str


class Undeliverable(Exception):
    """No allowed destination; retrying cannot help."""


async def dispatch_due(adapters: dict[Channel, ChannelAdapter], *, now: datetime | None = None,
                       limit: int = 20) -> int:
    """Send due outbox rows. Returns how many rows were handled."""
    now = now or utcnow()
    async with tx() as conn:
        rows = await fetch_all(
            conn,
            """select * from outbox where status = 'pending' and send_after <= :now
               order by created_at for update skip locked limit :limit""",
            now=now, limit=limit,
        )
        for row in rows:
            await _dispatch(conn, adapters, row, now)
        return len(rows)


async def _dispatch(conn: AsyncConnection, adapters: dict[Channel, ChannelAdapter],
                    row: dict[str, Any], now: datetime) -> None:
    try:
        if row["target"] == "household" and await _fan_out(conn, row):
            return
        destination = await _destination(conn, adapters, row)
        adapter = adapters[destination.channel]
        external_id, sent_text, meta = await _send(conn, adapter, destination, row)
    except Undeliverable as exc:
        await execute(conn, "update outbox set status = 'failed', last_error = :error where id = :id",
                      error=str(exc), id=row["id"])
        log.warning("outbox_undeliverable", household_id=row["household_id"], outbox_id=row["id"], reason=str(exc))
        return
    except Exception as exc:
        attempts = row["attempts"] + 1
        exhausted = attempts > len(BACKOFF_SECONDS)
        await execute(
            conn,
            """update outbox set attempts = :attempts, last_error = :error, status = :status,
                                 send_after = :send_after where id = :id""",
            attempts=attempts, error=str(exc)[:500], status="failed" if exhausted else "pending",
            send_after=now if exhausted else now + timedelta(seconds=BACKOFF_SECONDS[attempts - 1]),
            id=row["id"],
        )
        log.warning("outbox_send_failed", household_id=row["household_id"], outbox_id=row["id"],
                    attempts=attempts, error=type(exc).__name__)
        return

    await execute(
        conn,
        """update outbox set status = 'sent', sent_at = clock_timestamp(), channel_used = :channel,
                             external_id = :external_id, last_error = null where id = :id""",
        channel=destination.channel.value, external_id=external_id, id=row["id"],
    )
    await record_outbound(conn, row["household_id"], destination.thread_id, sent_text, meta, external_id)
    log.info("outbox_sent", household_id=row["household_id"], outbox_id=row["id"],
             channel=destination.channel.value)


async def record_outbound(conn: AsyncConnection, household_id: str, thread_id: str, text: str | None,
                          meta: dict[str, Any], external_id: str | None) -> None:
    """The agent's side of the conversation, kept in `messages` for thread history."""
    await execute(
        conn,
        """insert into messages (household_id, thread_id, direction, text, meta, external_id, status,
                                 created_at, processed_at)
           values (:h, :thread, 'out', :text, cast(:meta as jsonb), :external_id, 'sent',
                   clock_timestamp(), clock_timestamp())
           on conflict (thread_id, external_id) do nothing""",
        h=household_id, thread=thread_id, text=text, meta=jsonb(meta), external_id=external_id,
    )


async def _send(conn: AsyncConnection, adapter: ChannelAdapter, destination: Destination,
                row: dict[str, Any]) -> tuple[str | None, str | None, dict[str, Any]]:
    reply_to = None
    if row["reply_to_message_id"]:
        reply_to = await fetch_val(
            conn, "select external_id from messages where id = :id and thread_id = :thread",
            id=row["reply_to_message_id"], thread=destination.thread_id,
        )
    if row["react_emoji"]:
        emoji = adapter.capabilities.ack_emoji if row["react_emoji"] == "ack" else row["react_emoji"]
        try:
            if not (adapter.capabilities.reactions and reply_to):
                raise NotSupported
            await adapter.react(destination.external_thread_id, reply_to, emoji)
            return None, None, {"reaction": emoji}
        except NotSupported:
            sent = await adapter.send_text(destination.external_thread_id, emoji)
            return sent.external_id, emoji, {}
    external_id = None
    for index, chunk in enumerate(split_text(row["text"], adapter.capabilities.max_text_len)):
        sent = await adapter.send_text(destination.external_thread_id, adapter.format(chunk),
                                       reply_to if index == 0 else None)
        external_id = external_id or sent.external_id
    return external_id, row["text"], {}


async def _fan_out(conn: AsyncConnection, row: dict[str, Any]) -> bool:
    """A household send goes to the primary thread, else becomes one send per adult."""
    primary = await fetch_val(conn, "select primary_thread_id from households where id = :h",
                              h=row["household_id"])
    if primary:
        row["thread_id"] = primary
        return False
    adults = await fetch_all(conn, "select id from members where household_id = :h and role = 'adult'",
                             h=row["household_id"])
    for adult in adults:
        await enqueue(conn, OutboundMessage(
            household_id=row["household_id"], target="member", member_id=adult["id"], text=row["text"],
            react_emoji=row["react_emoji"], urgency=row["urgency"],
            respect_quiet_hours=row["respect_quiet_hours"],
            dedupe_key=f"{row['dedupe_key']}:{adult['id']}" if row["dedupe_key"] else None,
        ))
    await execute(conn, "update outbox set status = 'cancelled', last_error = 'sent per adult' where id = :id",
                  id=row["id"])
    return True


async def _destination(conn: AsyncConnection, adapters: dict[Channel, ChannelAdapter],
                       row: dict[str, Any]) -> Destination:
    household_id = row["household_id"]
    identities = await fetch_all(
        conn,
        """select ci.member_id, ci.channel, ci.handle, m.preferred_channel
           from channel_identities ci join members m on m.id = ci.member_id
           where m.household_id = :h""",
        h=household_id,
    )
    if row["target"] == "member":
        mine = sorted(
            (i for i in identities if i["member_id"] == row["member_id"] and Channel(i["channel"]) in adapters),
            key=lambda i: (i["channel"] != i["preferred_channel"], CHANNEL_ORDER.index(Channel(i["channel"]))),
        )
        if not mine:
            raise Undeliverable("member has no connected channel")
        channel = Channel(mine[0]["channel"])
        external = adapters[channel].dm_thread_id(mine[0]["handle"])
        thread_id = await fetch_val(
            conn,
            """insert into threads (household_id, channel, external_thread_id, scope)
               values (:h, :channel, :external, 'dm')
               on conflict (channel, external_thread_id) do update set scope = threads.scope
               returning id""",
            h=household_id, channel=channel.value, external=external,
        )
        return Destination(channel, external, str(thread_id))

    thread = await fetch_one(
        conn, "select id, channel, external_thread_id, scope from threads where id = :id and household_id = :h",
        id=row["thread_id"], h=household_id,
    )
    if thread is None or thread["channel"] not in {c.value for c in adapters}:
        raise Undeliverable("thread is not one of this household's connected threads")
    channel = Channel(thread["channel"])
    if thread["scope"] == "dm" and not any(
        i["channel"] == channel.value and adapters[channel].dm_thread_id(i["handle"]) == thread["external_thread_id"]
        for i in identities
    ):
        raise Undeliverable("thread does not belong to a verified member")
    return Destination(channel, thread["external_thread_id"], thread["id"])
