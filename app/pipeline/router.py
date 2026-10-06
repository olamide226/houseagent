"""Outbound router (spec section 7.3). Every send is an `outbox` row; this dispatches them.

Destinations are resolved only from the household's own `threads` and `channel_identities`
rows, so nothing can be addressed to anyone outside the family.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncConnection

from app.channels.base import ChannelAdapter, NotSupported, PermanentError
from app.core.envelope import Channel, DeliveryStatus, OutboundMessage
from app.core.timeutil import utcnow
from app.db import execute, fetch_all, fetch_one, fetch_val, jsonb, tx
from app.services import members

log = structlog.get_logger()

BACKOFF_SECONDS = [10, 30, 120, 600, 1800]
CHANNEL_ORDER = [Channel.telegram, Channel.whatsapp, Channel.imessage]
TEMPLATE_TEXT_MAX = 900   # what fits in the template's one parameter
FALLBACK = "fallback:"    # dedupe-key prefix of a send that is itself a second try on another channel


async def enqueue(conn: AsyncConnection, message: OutboundMessage, *, status: str = "pending",
                  send_after: datetime | None = None) -> str | None:
    """Queue a send inside the caller's transaction. None when `dedupe_key` was already queued."""
    row_id = await fetch_val(
        conn,
        """insert into outbox (household_id, target, thread_id, member_id, text, react_emoji,
                               reply_to_message_id, urgency, respect_quiet_hours, dedupe_key, status,
                               created_at, send_after)
           values (:household_id, :target, :thread_id, :member_id, :text, :react_emoji,
                   :reply_to_message_id, :urgency, :respect_quiet_hours, :dedupe_key, :status,
                   clock_timestamp(), coalesce(cast(:send_after as timestamptz), clock_timestamp()))
           on conflict (household_id, dedupe_key) do nothing returning id""",
        **message.model_dump(), status=status, send_after=send_after,
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


async def failed_since(conn: AsyncConnection, household_id: str, since: datetime) -> dict[str, int]:
    """Sends that failed for good since `since`, counted by the channel they were tried on."""
    rows = await fetch_all(
        conn, "select channel_used, count(*) as n from outbox where household_id = :h and status = 'failed' "
              "and channel_used is not null and created_at > :since group by channel_used",
        h=household_id, since=since)
    return {row["channel_used"]: row["n"] for row in rows}


async def last_sent(conn: AsyncConnection, household_id: str) -> dict[str, datetime]:
    rows = await fetch_all(
        conn, "select channel_used, max(sent_at) as at from outbox where household_id = :h and status = 'sent' "
              "group by channel_used", h=household_id)
    return {row["channel_used"]: row["at"] for row in rows}


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
    member_id: str | None   # whose DM it is; None for a group


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
    destination = None
    try:
        if row["target"] == "household" and await _fan_out(conn, row, now):
            return
        destination = await _destination(conn, adapters, row)
        if row["respect_quiet_hours"] and row["urgency"] != "high":
            # Held while the recipient, or for a group any adult, is in quiet hours.
            held_until = await members.quiet_until(conn, row["household_id"], destination.member_id, now)
            if held_until is not None:
                await execute(conn, "update outbox set send_after = :until where id = :id",
                              until=held_until, id=row["id"])
                log.info("outbox_held_for_quiet_hours", household_id=row["household_id"], outbox_id=row["id"])
                return
        adapter = adapters[destination.channel]
        external_id, sent_text, meta = await _send(conn, adapter, destination, row, now)
    except Undeliverable as exc:
        await execute(conn, "update outbox set status = 'failed', last_error = :error where id = :id",
                      error=str(exc), id=row["id"])
        log.warning("outbox_undeliverable", household_id=row["household_id"], outbox_id=row["id"], reason=str(exc))
        return
    except Exception as exc:
        attempts = row["attempts"] + 1
        gave_up = isinstance(exc, PermanentError) or attempts > len(BACKOFF_SECONDS)
        await execute(
            conn,
            """update outbox set attempts = :attempts, last_error = :error, status = :status,
                                 send_after = :send_after, channel_used = :channel where id = :id""",
            attempts=attempts, error=str(exc)[:500], status="failed" if gave_up else "pending",
            send_after=now if gave_up else now + timedelta(seconds=BACKOFF_SECONDS[attempts - 1]),
            channel=destination.channel.value if destination else None, id=row["id"],
        )
        log.warning("outbox_send_failed", household_id=row["household_id"], outbox_id=row["id"],
                    attempts=attempts, error=type(exc).__name__)
        if gave_up and destination:
            await _fall_back(conn, adapters, row, destination.channel, destination.member_id, now)
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


async def delivery_failed(conn: AsyncConnection, adapters: dict[Channel, ChannelAdapter],
                          status: DeliveryStatus, now: datetime | None = None) -> bool:
    """A channel accepted a send and later reported that it never arrived (a WhatsApp status).
    The send is marked failed and tried once on the member's next channel. False if it is not ours."""
    row = await fetch_one(
        conn, "select * from outbox where channel_used = :channel and external_id = :external "
              "and status = 'sent' for update",
        channel=status.channel.value, external=status.external_message_id,
    )
    if row is None:
        return False   # not one of our sends, or a repeat of a failure already handled
    await execute(conn, "update outbox set status = 'failed', last_error = :error where id = :id",
                  error=(status.error or "the channel reported the send failed")[:500], id=row["id"])
    log.warning("outbox_delivery_failed", household_id=row["household_id"], outbox_id=row["id"],
                channel=status.channel.value)
    try:
        member_id = (await _destination(conn, adapters, row)).member_id   # whose DM it was, if anyone's
    except Undeliverable:
        return True
    await _fall_back(conn, adapters, row, status.channel, member_id, now or utcnow())
    return True


async def _fall_back(conn: AsyncConnection, adapters: dict[Channel, ChannelAdapter], row: dict[str, Any],
                     failed: Channel, member_id: str | None, now: datetime) -> None:
    """After a send to `member_id` has failed for good on `failed`, try their next connected
    channel, once (spec 7.3 step 6). Only a text to one person moves: an ack means nothing in
    another chat, and a group has no next channel."""
    if member_id is None or not row["text"] or (row["dedupe_key"] or "").startswith(FALLBACK):
        return
    identities = await fetch_all(
        conn, "select channel, handle from channel_identities where member_id = :m", m=member_id)
    others = sorted(
        (i for i in identities if Channel(i["channel"]) in adapters and i["channel"] != failed.value),
        key=lambda i: CHANNEL_ORDER.index(Channel(i["channel"])),
    )
    if not others:
        return
    channel = Channel(others[0]["channel"])
    thread_id = await _dm_thread(conn, row["household_id"], channel,
                                 adapters[channel].dm_thread_id(others[0]["handle"]))
    await enqueue(conn, OutboundMessage(
        household_id=row["household_id"], target="thread", thread_id=thread_id, text=row["text"],
        urgency=row["urgency"], respect_quiet_hours=row["respect_quiet_hours"],
        dedupe_key=f"{FALLBACK}{row['id']}",
    ), send_after=now)
    log.info("outbox_fallback_queued", household_id=row["household_id"], outbox_id=row["id"], channel=channel.value)


async def record_outbound(conn: AsyncConnection, household_id: str, thread_id: str, text: str | None,
                          meta: dict[str, Any], external_id: str | None, *, at: datetime | None = None) -> None:
    """The agent's side of the conversation, kept in `messages` for thread history."""
    await execute(
        conn,
        """insert into messages (household_id, thread_id, direction, text, meta, external_id, status,
                                 created_at, processed_at)
           values (:h, :thread, 'out', :text, cast(:meta as jsonb), :external_id, 'sent',
                   coalesce(cast(:at as timestamptz), clock_timestamp()), clock_timestamp())
           on conflict (thread_id, external_id) do nothing""",
        h=household_id, thread=thread_id, text=text, meta=jsonb(meta), external_id=external_id, at=at,
    )


async def _send(conn: AsyncConnection, adapter: ChannelAdapter, destination: Destination,
                row: dict[str, Any], now: datetime) -> tuple[str | None, str | None, dict[str, Any]]:
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
    template = adapter.capabilities.proactive_template
    if template and not await _window_open(conn, adapter, destination, now):
        # Free-form text would be refused: it goes inside the approved template instead.
        text = row["text"] if len(row["text"]) <= TEMPLATE_TEXT_MAX else row["text"][:TEMPLATE_TEXT_MAX - 1] + "…"
        try:
            sent = await adapter.send_template(destination.external_thread_id, template, [text])
            return sent.external_id, row["text"], {"template": template}
        except NotSupported:
            pass
    external_id = None
    for index, chunk in enumerate(split_text(row["text"], adapter.capabilities.max_text_len)):
        sent = await adapter.send_text(destination.external_thread_id, adapter.format(chunk),
                                       reply_to if index == 0 else None)
        external_id = external_id or sent.external_id
    return external_id, row["text"], {}


async def last_heard(conn: AsyncConnection, destination: Destination) -> datetime | None:
    """When the other side last wrote in this thread; connecting with an invite code counts,
    though the code itself is never stored as a message."""
    heard: datetime | None = await fetch_val(
        conn,
        """select greatest((select max(created_at) from messages where thread_id = :thread and direction = 'in'),
                           (select verified_at from channel_identities
                            where member_id = cast(:member as uuid) and channel = :channel))""",
        thread=destination.thread_id, member=destination.member_id, channel=destination.channel.value,
    )
    return heard


async def _window_open(conn: AsyncConnection, adapter: ChannelAdapter, destination: Destination,
                       now: datetime) -> bool:
    """Whether free-form text may be sent now: always, unless the channel only allows it for a
    number of hours after the other side last wrote (WhatsApp: 24)."""
    hours = adapter.capabilities.proactive_window_hours
    if hours is None:
        return True
    heard = await last_heard(conn, destination)
    return heard is not None and now - heard < timedelta(hours=hours)


async def _fan_out(conn: AsyncConnection, row: dict[str, Any], now: datetime) -> bool:
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
        ), send_after=now)
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
        return Destination(channel, external, await _dm_thread(conn, household_id, channel, external),
                           row["member_id"])

    thread = await fetch_one(
        conn, "select id, channel, external_thread_id, scope from threads where id = :id and household_id = :h",
        id=row["thread_id"], h=household_id,
    )
    if thread is None or thread["channel"] not in {c.value for c in adapters}:
        raise Undeliverable("thread is not one of this household's connected threads")
    channel = Channel(thread["channel"])
    if thread["scope"] == "group":
        return Destination(channel, thread["external_thread_id"], thread["id"], None)
    owner = next((i["member_id"] for i in identities if i["channel"] == channel.value
                  and adapters[channel].dm_thread_id(i["handle"]) == thread["external_thread_id"]), None)
    if owner is None:
        raise Undeliverable("thread does not belong to a verified member")
    return Destination(channel, thread["external_thread_id"], thread["id"], owner)


async def _dm_thread(conn: AsyncConnection, household_id: str, channel: Channel, external: str) -> str:
    return str(await fetch_val(
        conn,
        """insert into threads (household_id, channel, external_thread_id, scope)
           values (:h, :channel, :external, 'dm')
           on conflict (channel, external_thread_id) do update set scope = threads.scope
           returning id""",
        h=household_id, channel=channel.value, external=external,
    ))
