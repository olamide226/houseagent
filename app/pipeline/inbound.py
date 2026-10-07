"""Inbound pipeline (spec sections 7.1 and 7.2): persist, dedupe, debounce, dispatch to the agent."""
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any, Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncConnection

from app.agent.base import AgentResult, AgentRuntime, Ctx
from app.agent.loop import message_lines
from app.channels.base import ChannelAdapter
from app.core.envelope import Channel, Envelope, GroupUpdate, InboundEvent, MediaRef, OutboundMessage
from app.core.identity import member_for_handle, parse_invite_code, redeem_invite
from app.core.timeutil import utcnow
from app.db import advisory_lock, execute, fetch_all, fetch_one, fetch_val, jsonb, tx
from app.llm.stt import SpeechToText
from app.media.store import MediaStore
from app.pipeline import media as media_pipeline
from app.pipeline.router import delivery_failed, enqueue, record_outbound
from app.services import households, members

log = structlog.get_logger()

SORRY = "Sorry, that didn't go through, try again?"
PLAYGROUND = "playground"   # threads.channel for dashboard Playground and eval turns
# Said once, by code, when an adult connects: the first setup question, or a one-line welcome.
FIRST_QUESTION = ("Let's get you set up, one question at a time; say skip to pass on any. "
                  "Who lives here, including the kids?")
WELCOME = "Just tell me what's run out, what to buy or what's coming up, and I'll keep track."
INVITE_ATTEMPTS_PER_HOUR = 5
_invite_attempts: dict[str, deque[float]] = defaultdict(deque)


# ---------------------------------------------------------------- webhook side (api process)
async def receive(adapter: ChannelAdapter, body: bytes, adapters: dict[Channel, ChannelAdapter]) -> None:
    """Parse a verified webhook body and persist its events. Only database errors propagate."""
    try:
        events = await adapter.parse(body)
        updates = await adapter.parse_updates(body)
    except Exception as exc:
        log.warning("webhook_parse_failed", channel=adapter.channel.value, error=type(exc).__name__)
        return
    for event in events:
        async with tx() as conn:
            await ingest(conn, adapter, event)
    for update in updates:
        async with tx() as conn:
            if isinstance(update, GroupUpdate):
                await households.finish_group(conn, update)
            elif update.status == "failed":
                await delivery_failed(conn, adapters, update)


async def ingest(conn: AsyncConnection, adapter: ChannelAdapter, event: InboundEvent) -> None:
    member = await member_for_handle(conn, event.channel, event.sender_handle)
    if member is None:
        await _unknown_sender(conn, adapter, event)
        return
    thread_id = await _upsert_thread(conn, member["household_id"], event.channel.value,
                                     event.external_thread_id, event.scope)
    if thread_id is None:
        return
    if event.scope == "group":
        await execute(conn, "update households set primary_thread_id = :t "
                            "where id = :h and primary_thread_id is null", t=thread_id, h=member["household_id"])
    meta = {
        "reply_to_external_id": event.reply_to_external_id,
        "reaction_emoji": event.reaction_emoji,
        "reaction_target_external_id": event.reaction_target_external_id,
    }
    message_id = await fetch_val(
        conn,
        """insert into messages (household_id, thread_id, member_id, direction, text, media, meta, external_id)
           values (:h, :thread, :member, 'in', :text, cast(:media as jsonb), cast(:meta as jsonb), :external)
           on conflict (thread_id, external_id) do nothing returning id""",
        h=member["household_id"], thread=thread_id, member=member["id"], text=event.text,
        media=jsonb([m.model_dump(exclude_none=True) for m in event.media]),
        meta=jsonb({k: v for k, v in meta.items() if v is not None}), external=event.external_message_id,
    )
    if message_id is None:
        return   # a provider retry: already stored
    await execute(conn, "select pg_notify('inbound', :h)", h=member["household_id"])
    log.info("message_received", household_id=member["household_id"], message_id=str(message_id),
             channel=event.channel.value)


async def _unknown_sender(conn: AsyncConnection, adapter: ChannelAdapter, event: InboundEvent) -> None:
    """Unknown senders are invisible: nothing stored, nothing sent, unless they hold an invite code."""
    code = parse_invite_code(event.text)
    if code is None or event.scope != "dm":
        log.info("unknown_sender_ignored", channel=event.channel.value)
        return
    attempts = _invite_attempts[f"{event.channel.value}:{event.sender_handle}"]
    now = time.monotonic()
    while attempts and now - attempts[0] > 3600:
        attempts.popleft()
    if len(attempts) >= INVITE_ATTEMPTS_PER_HOUR:
        log.warning("invite_rate_limited", channel=event.channel.value)
        return
    attempts.append(now)
    member = await redeem_invite(conn, event.channel, event.sender_handle, code, utcnow())
    if member is None:
        log.info("invite_rejected", channel=event.channel.value)
        return
    await members.record_connection(conn, member, event.channel.value)
    thread_id = await _upsert_thread(conn, member["household_id"], event.channel.value,
                                     adapter.dm_thread_id(event.sender_handle), "dm")
    # Setup starts with whoever connects while the first step is open; anyone later is just welcomed.
    setting_up = (await households.onboarding(conn, member["household_id"]))["step"] == "family"
    await enqueue(conn, OutboundMessage(
        household_id=member["household_id"], target="thread", thread_id=thread_id,
        text=f"Hi {member['name']}, you're connected. {FIRST_QUESTION if setting_up else WELCOME}",
        respect_quiet_hours=False,
    ))
    if not setting_up and await households.presence_offered(conn, member["household_id"]):
        # Setup already made the others the shop-arrival offer; this adult's follows the welcome.
        await households.offer_shops(conn, member["household_id"], member_id=member["id"], speaker=member["id"])
    log.info("invite_redeemed", household_id=member["household_id"], channel=event.channel.value)


async def _upsert_thread(conn: AsyncConnection, household_id: str, channel: str, external_thread_id: str,
                         scope: str) -> str | None:
    thread = await fetch_one(
        conn,
        """insert into threads (household_id, channel, external_thread_id, scope)
           values (:h, :channel, :external, :scope)
           on conflict (channel, external_thread_id) do update set scope = excluded.scope
           returning id, household_id""",
        h=household_id, channel=channel, external=external_thread_id, scope=scope,
    )
    assert thread is not None
    return thread["id"] if thread["household_id"] == household_id else None


# ---------------------------------------------------------------- processing side (worker)
async def ready_households(conn: AsyncConnection, debounce_seconds: float) -> list[str]:
    """Households whose newest unprocessed message has been quiet for the debounce window."""
    rows = await fetch_all(
        conn,
        """select household_id from messages where status = 'received' and direction = 'in'
           group by household_id
           having max(created_at) <= clock_timestamp() - make_interval(secs => :debounce)""",
        debounce=debounce_seconds,
    )
    return [row["household_id"] for row in rows]


async def seconds_until_ready(conn: AsyncConnection, debounce_seconds: float) -> float | None:
    """How long until the next waiting batch has settled; None when nothing is waiting."""
    wait = await fetch_val(
        conn,
        """select extract(epoch from min(newest) + make_interval(secs => :debounce) - clock_timestamp())
           from (select max(created_at) as newest from messages
                 where status = 'received' and direction = 'in' group by household_id) pending""",
        debounce=debounce_seconds,
    )
    return None if wait is None else max(float(wait), 0.0)


def group_by_thread(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """One batch per thread, oldest first within a batch, batches ordered by first message."""
    batches: dict[str, list[dict[str, Any]]] = {}
    for message in messages:
        batches.setdefault(message["thread_id"], []).append(message)
    return list(batches.values())


def build_envelope(batch: list[dict[str, Any]], reaction_targets: dict[str, str] | None = None,
                   reply_to_text: str | None = None) -> Envelope:
    """Assemble what the agent reads from one thread's debounced batch."""
    last = batch[-1]
    group = last["scope"] == "group"
    lines: list[str] = []
    for message in batch:
        target = (reaction_targets or {}).get(message["meta"].get("reaction_target_external_id", ""))
        for line in message_lines(message, target):
            lines.append(f"{message['member_name']}: {line}" if group else line)
    channel = last["channel"]
    return Envelope(
        household_id=last["household_id"], member_id=last["member_id"], member_name=last["member_name"],
        thread_id=last["thread_id"], message_ids=[m["id"] for m in batch],
        channel=Channel(channel) if channel != PLAYGROUND else None, scope=last["scope"],
        text="\n".join(lines),
        images=[MediaRef.model_validate(m) for message in batch for m in message["media"] if m["kind"] == "image"],
        reply_to_text=reply_to_text, received_at=last["created_at"],
    )


async def _waiting_messages(conn: AsyncConnection, column: str, value: str) -> list[dict[str, Any]]:
    """Claim the unprocessed inbound messages of a household or a thread, oldest first."""
    assert column in ("household_id", "thread_id")
    return await fetch_all(
        conn,
        f"""select m.id, m.household_id, m.thread_id, m.member_id, m.text, m.media, m.meta, m.created_at,
                   t.channel, t.scope, mem.name as member_name
            from messages m join threads t on t.id = m.thread_id join members mem on mem.id = m.member_id
            where m.{column} = :value and m.status = 'received' and m.direction = 'in'
            order by m.created_at, m.id
            for update of m skip locked""",
        value=value,
    )


async def process_household(household_id: str, runtime: AgentRuntime, adapters: dict[Channel, ChannelAdapter],
                            *, stt: SpeechToText | None = None, media: MediaStore | None = None,
                            public_base_url: str = "") -> int:
    """Run one turn per thread for the household's waiting messages. Returns the number of turns.

    The whole household is serialised by an advisory lock held for the transaction, so two
    messages never race on stock."""
    turns = 0
    async with tx() as conn:
        await advisory_lock(conn, household_id)
        messages = await _waiting_messages(conn, "household_id", household_id)
        await execute(conn, "update messages set status = 'processing' where id = any(cast(:ids as uuid[]))",
                      ids=[m["id"] for m in messages])
        messages = [m for m in messages if not await _keyword(conn, m, public_base_url)]
        for batch in group_by_thread(messages):
            ids = [m["id"] for m in batch]
            # Outside the turn's savepoint: what was stored stays recorded even if the turn fails,
            # so retention can still find and delete it.
            await _prepare_media(conn, batch, adapters, stt, media)
            try:
                async with conn.begin_nested():
                    envelope = build_envelope(batch, await _reaction_targets(conn, batch),
                                              await _reply_to_text(conn, batch[-1]))
                    await run_turn(conn, envelope, runtime)
            except Exception as exc:
                log.exception("turn_failed", household_id=household_id, message_id=ids[-1])
                await _fail_turn(conn, batch, f"{type(exc).__name__}: {exc}")
            turns += 1
    return turns


async def run_turn(conn: AsyncConnection, envelope: Envelope, runtime: AgentRuntime, *,
                   simulated: bool = False) -> AgentResult:
    """Agent turn plus the response policy (spec 7.2 steps 5 to 7). Shared with simulate_turn."""
    last = envelope.message_ids[-1]
    ctx = Ctx(conn=conn, household_id=envelope.household_id, member_id=envelope.member_id,
              thread_id=envelope.thread_id, message_id=last, now=envelope.received_at)
    started = time.monotonic()
    result = await runtime.handle(envelope, ctx)
    latency_ms = round((time.monotonic() - started) * 1000)

    outbox_id = None
    if not result.noop:
        reply = OutboundMessage(
            household_id=envelope.household_id, target="thread", thread_id=envelope.thread_id,
            respect_quiet_hours=False,   # a direct answer to someone who is awake and asking
        )
        if result.ack_only:
            reply.react_emoji, reply.reply_to_message_id = "ack", last
        else:
            reply.text = result.reply
            reply.reply_to_message_id = last if len(envelope.message_ids) > 1 else None
        outbox_id = await enqueue(conn, reply, status="simulated" if simulated else "pending")
        if simulated and envelope.thread_id:
            await record_outbound(conn, envelope.household_id, envelope.thread_id, reply.text,
                                  {"reaction": "ack"} if result.ack_only else {}, None,
                                  at=envelope.received_at + timedelta(milliseconds=1))

    await execute(
        conn,
        "update messages set status = 'processed', processed_at = clock_timestamp() "
        "where id = any(cast(:ids as uuid[]))", ids=envelope.message_ids,
    )
    turn = {
        "usage": result.usage.model_dump(), "latency_ms": latency_ms, "outbox_id": outbox_id,
        "outcome": "noop" if result.noop else "ack" if result.ack_only else "reply",
        "tool_calls": [call.model_dump() for call in result.tool_calls],
    }
    await execute(conn, "update messages set meta = meta || cast(:turn as jsonb) where id = :id",
                  turn=jsonb({"turn": turn, "usage": turn["usage"]}), id=last)
    log.info("turn_done", household_id=envelope.household_id, message_id=last, outcome=turn["outcome"],
             tool_calls=len(result.tool_calls), latency_ms=latency_ms, **result.usage.model_dump())
    return result


async def simulate_turn(conn: AsyncConnection, runtime: AgentRuntime, household_id: str, member_id: str,
                        text: str | None, *, scope: Literal["dm", "group"] = "dm", now: datetime | None = None,
                        photos: list[MediaRef] | None = None) -> AgentResult:
    """Run one turn as `member` without a channel: the Playground and the eval suite use this.

    Everything happens on `conn`; the caller commits to apply it or rolls back for a dry run.
    `now` pins when the message arrived, so evals do not depend on the day they are run.
    `photos` are refs already in the runtime's MediaStore."""
    external = f"{PLAYGROUND}:{household_id if scope == 'group' else member_id}"
    thread_id = await _upsert_thread(conn, household_id, PLAYGROUND, external, scope)
    await execute(
        conn,
        """insert into messages (household_id, thread_id, member_id, direction, text, media, external_id,
                                 created_at)
           values (:h, :thread, :member, 'in', :text, cast(:media as jsonb), :external,
                   coalesce(cast(:now as timestamptz), clock_timestamp()))""",
        h=household_id, thread=thread_id, member=member_id, text=text, external=str(uuid.uuid4()), now=now,
        media=jsonb([photo.model_dump(exclude_none=True) for photo in photos or []]),
    )
    assert thread_id is not None
    batch = await _waiting_messages(conn, "thread_id", thread_id)
    return await run_turn(conn, build_envelope(batch), runtime, simulated=True)


async def _keyword(conn: AsyncConnection, message: dict[str, Any], public_base_url: str) -> bool:
    """Two exact words are answered by code, never by the agent, because the answer holds a secret:
    "dashboard" with a one-time login link, "shops" with the person's link for arriving at a shop."""
    word = (message["text"] or "").strip().lower()
    if word == "dashboard":
        token = await members.create_login_token(conn, message["member_id"], utcnow())
        text = (f"Your dashboard link (valid 10 minutes, works once): {public_base_url}/login/{token}"
                if token else "Too many login links for now. Try again in an hour.")
    elif word == "shops":
        text = await households.shops_link(conn, message["member_id"])
    else:
        return False
    await enqueue(conn, OutboundMessage(household_id=message["household_id"], target="member",
                                        member_id=message["member_id"], text=text, respect_quiet_hours=False))
    await execute(conn, "update messages set status = 'processed', processed_at = clock_timestamp() "
                        "where id = :id", id=message["id"])
    return True


async def _prepare_media(conn: AsyncConnection, batch: list[dict[str, Any]], adapters: dict[Channel, ChannelAdapter],
                         stt: SpeechToText | None, store: MediaStore | None) -> None:
    for message in batch:
        adapter = adapters.get(Channel(message["channel"])) if message["channel"] != PLAYGROUND else None
        if await media_pipeline.prepare(message["media"], message["household_id"], message["id"],
                                        adapter, stt, store):
            await execute(conn, "update messages set media = cast(:media as jsonb) where id = :id",
                          media=jsonb(message["media"]), id=message["id"])


async def _reaction_targets(conn: AsyncConnection, batch: list[dict[str, Any]]) -> dict[str, str]:
    targets = [m["meta"]["reaction_target_external_id"] for m in batch
               if m["meta"].get("reaction_target_external_id")]
    if not targets:
        return {}
    rows = await fetch_all(
        conn, "select external_id, text from messages where thread_id = :t and external_id = any(:ids)",
        t=batch[0]["thread_id"], ids=targets,
    )
    return {row["external_id"]: row["text"] or "" for row in rows}


async def _reply_to_text(conn: AsyncConnection, message: dict[str, Any]) -> str | None:
    external = message["meta"].get("reply_to_external_id")
    if not external:
        return None
    text = await fetch_val(conn, "select text from messages where thread_id = :t and external_id = :e",
                           t=message["thread_id"], e=external)
    return None if text is None else str(text)


async def _fail_turn(conn: AsyncConnection, batch: list[dict[str, Any]], error: str) -> None:
    """Mark the batch failed and apologise once. The turn's own writes were already rolled back."""
    await execute(
        conn,
        """update messages set status = 'failed', processed_at = clock_timestamp(),
                               meta = meta || cast(:error as jsonb)
           where id = any(cast(:ids as uuid[]))""",
        error=jsonb({"error": error[:500]}), ids=[m["id"] for m in batch],
    )
    await enqueue(conn, OutboundMessage(
        household_id=batch[0]["household_id"], target="thread", thread_id=batch[0]["thread_id"],
        text=SORRY, respect_quiet_hours=False,
    ))
