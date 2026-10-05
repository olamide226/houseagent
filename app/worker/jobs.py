"""Worker jobs (spec section 10): inbound processing, outbox dispatch, reminders, recurrence,
the two digests and media retention.

Every job may run twice, or on two replicas at once: rows are claimed with SKIP LOCKED,
outbox dedupe keys and unique indexes make the inserts idempotent, and the once-a-day jobs
claim a `job_runs` row first.
"""
import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncConnection

from app.agent.base import AgentRuntime, ToolError
from app.channels.base import ChannelAdapter
from app.config import Settings
from app.core.envelope import Channel, MediaRef, OutboundMessage
from app.core.timeutil import day_bounds, local, next_occurrence, occurrences, utcnow
from app.db import advisory_lock, engine, execute, fetch_all, jsonb, tx
from app.llm.stt import SpeechToText
from app.media.store import MediaStore
from app.pipeline import inbound, router
from app.pipeline import media as media_pipeline
from app.services import calendar, inventory, shopping

log = structlog.get_logger()
POLL_SECONDS = 2.0
REMINDERS_SECONDS = 15.0
RECURRENCE_SECONDS = 3600.0
DIGEST_POLL_SECONDS = 60.0
DIGEST_GRACE = timedelta(hours=4)       # a digest missed by more than this is skipped, not sent late
WEEKLY_DIGEST_AT = time(18, 0)          # on Sundays, household time
MEDIA_CLEANUP_SECONDS = 3600.0
LATE_MINUTES = 10                       # a reminder this long after its event began is dropped


async def inbound_job(settings: Settings, runtime: AgentRuntime, adapters: dict[Channel, ChannelAdapter],
                      stt: SpeechToText | None, media: MediaStore | None, outbox_wake: asyncio.Event) -> None:
    """Process settled batches. Wakes on NOTIFY inbound, when a batch's debounce ends, or every 2 s."""
    wake = asyncio.Event()
    async with engine().connect() as listening:
        listener = (await listening.get_raw_connection()).driver_connection
        assert listener is not None
        await listener.add_listener("inbound", lambda *_: wake.set())
        try:
            while True:
                wake.clear()
                async with tx() as conn:
                    ready = await inbound.ready_households(conn, settings.debounce_seconds)
                for household_id in ready:
                    await inbound.process_household(household_id, runtime, adapters, stt=stt, media=media,
                                                    public_base_url=settings.public_base_url)
                    outbox_wake.set()
                async with tx() as conn:
                    pending = await inbound.seconds_until_ready(conn, settings.debounce_seconds)
                timeout = POLL_SECONDS if pending is None else min(POLL_SECONDS, pending)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), timeout)
        finally:
            await listening.invalidate()   # it is still listening: close it, never return it to the pool


async def outbox_job(adapters: dict[Channel, ChannelAdapter], wake: asyncio.Event) -> None:
    """Dispatch due sends every 2 s, or at once when a turn has just queued a reply."""
    while True:
        wake.clear()
        while await router.dispatch_due(adapters):
            pass
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), POLL_SECONDS)


async def every(seconds: float, job: Callable[[], Awaitable[int]], wake: asyncio.Event) -> None:
    """Run a job on a fixed cadence, waking the outbox whenever it queued something."""
    while True:
        if await job():
            wake.set()
        await asyncio.sleep(seconds)


# ---------------------------------------------------------------- reminders and recurrence
def _event_start(reminder: dict[str, Any]) -> datetime | None:
    """When the occurrence an event reminder is for begins; None for a standalone reminder."""
    start: datetime | None = reminder["event_starts_at"]
    if reminder["series_rrule"] is None:
        return start
    for lead in sorted(reminder["leads"]):
        start = reminder["fire_at"] + timedelta(minutes=lead)
        if occurrences(reminder["series_rrule"], reminder["event_starts_at"], reminder["timezone"], start,
                       start + timedelta(seconds=1)):
            return start
    return None


async def fire_reminders(now: datetime | None = None, *, limit: int = 100) -> int:
    """Move due reminders to the outbox. One-off: `sent`. Repeating: stays `scheduled` at its
    next occurrence. Returns how many were queued."""
    now = now or utcnow()
    queued = 0
    async with tx() as conn:
        due = await fetch_all(
            conn,
            """select r.id, r.household_id, r.target, r.member_id, r.text, r.fire_at, r.rrule, r.urgency,
                      h.timezone, e.starts_at as event_starts_at, e.rrule as series_rrule,
                      e.remind_before_minutes as leads,
                      exists (select 1 from reminders later
                              where later.event_id = r.event_id and later.status = 'scheduled'
                                and later.fire_at > r.fire_at and later.fire_at <= :now) as superseded
               from reminders r join households h on h.id = r.household_id
               left join events e on e.id = r.event_id
               where r.status = 'scheduled' and r.fire_at <= :now
               order by r.fire_at for update of r skip locked limit :limit""",
            now=now, limit=limit,
        )
        for reminder in due:
            started = _event_start(reminder)
            if reminder["superseded"] or (started is not None and started + timedelta(minutes=LATE_MINUTES) < now):
                # The worker was down: a later reminder for the same event is due too, or the event
                # has begun. Saying it now would only be noise.
                await execute(conn, "update reminders set status = 'cancelled' where id = :id", id=reminder["id"])
                continue
            if await router.enqueue(conn, OutboundMessage(
                household_id=reminder["household_id"], target=reminder["target"], member_id=reminder["member_id"],
                text=reminder["text"], urgency=reminder["urgency"],
                dedupe_key=f"reminder:{reminder['id']}:{reminder['fire_at'].isoformat()}",
            ), send_after=now):
                queued += 1
            upcoming = reminder["rrule"] and next_occurrence(
                reminder["rrule"], reminder["fire_at"], reminder["timezone"], max(now, reminder["fire_at"]))
            if upcoming:
                await execute(conn, "update reminders set fire_at = :next where id = :id",
                              next=upcoming, id=reminder["id"])
            else:
                await execute(conn, "update reminders set status = 'sent', sent_at = :now where id = :id",
                              now=now, id=reminder["id"])
            log.info("reminder_queued", household_id=reminder["household_id"], reminder_id=reminder["id"])
    return queued


async def expand_recurrence(now: datetime | None = None) -> int:
    """Give every active recurring event its reminder rows for the next 48 hours. The unique
    `(event_id, fire_at)` index makes a second run insert nothing."""
    now = now or utcnow()
    async with tx() as conn:
        events = await fetch_all(
            conn, "select id, household_id from events where status = 'active' and rrule is not null")
    created = 0
    for event in events:
        async with tx() as conn:
            await advisory_lock(conn, event["household_id"])
            with contextlib.suppress(ToolError):   # the event was undone a moment ago
                created += await calendar.materialise(conn, event["household_id"], event["id"], now)
    return created


# ---------------------------------------------------------------- digests
async def _once_per_household(job: str, now: datetime,
                              run_key: Callable[[datetime, dict[str, Any]], str | None],
                              build: Callable[[AsyncConnection, dict[str, Any], datetime], Awaitable[str | None]],
                              ) -> int:
    """Send one household message per `run_key`, claimed in `job_runs` so a restart or a
    second replica never doubles it. `run_key` returns None while the job is not due."""
    async with tx() as conn:
        households = await fetch_all(conn, "select id, timezone, digest_time from households")
    sent = 0
    for household in households:
        key = run_key(local(now, household["timezone"]), household)
        if key is None:
            continue
        async with tx() as conn:
            if not await execute(
                conn, "insert into job_runs (job, household_id, run_key) values (:job, :h, :key) "
                      "on conflict do nothing", job=job, h=household["id"], key=key):
                continue
            text = await build(conn, household, now)
            if text and await router.enqueue(conn, OutboundMessage(
                household_id=household["id"], target="household", text=text, urgency="low",
                dedupe_key=f"{job}:{key}",
            ), send_after=now):
                sent += 1
                log.info("digest_queued", job=job, household_id=household["id"], run_key=key)
    return sent


def _due(here: datetime, at: time) -> bool:
    scheduled = datetime.combine(here.date(), at, tzinfo=here.tzinfo)
    return scheduled <= here < scheduled + DIGEST_GRACE


def _section(title: str, lines: list[str]) -> list[str]:
    return [title, *(f"- {line}" for line in lines)] if lines else []


async def _daily_brief(conn: AsyncConnection, household: dict[str, Any], now: datetime) -> str | None:
    """Today's events and reminders and what to use up; nothing at all on an empty day."""
    timezone = household["timezone"]
    today = local(now, timezone).date()
    _, tonight = day_bounds(today, timezone)
    events = await calendar.occurrences_between(conn, household["id"], now, tonight)
    reminders = await calendar.standalone_reminders(conn, household["id"], now, tonight)
    expiring = await inventory.stock_rows(conn, household["id"], expiring_within_days=2, today=today)
    # Tomorrow's events whose day-before reminder fell in quiet hours are announced here instead.
    early = await calendar.brief_only(conn, household["id"], tonight, tonight + timedelta(days=1))
    lines = [
        *_section(f"Today, {today:%A %-d %B}:", [o.line(timezone, day=False) for o in events]),
        *_section("Reminders:", [f"{local(r['fire_at'], timezone):%H:%M} {r['text']}" for r in reminders]),
        *_section("Tomorrow:", [o.line(timezone, day=False) for o in early]),
        *_section("Use soon:", [f"{r['item']} ({r['location']}, {r['expires_on']:%-d %b})" for r in expiring]),
    ]
    return "\n".join(lines) or None


async def daily_brief(now: datetime | None = None) -> int:
    """Once a day at each household's `digest_time`, and only if something is due."""
    return await _once_per_household(
        "daily_brief", now or utcnow(),
        lambda here, household: f"{here:%Y-%m-%d}" if _due(here, household["digest_time"]) else None,
        _daily_brief,
    )


async def _weekly_digest(conn: AsyncConnection, household: dict[str, Any], now: datetime) -> str | None:
    timezone = household["timezone"]
    events = await calendar.occurrences_between(conn, household["id"], now, now + timedelta(days=7))
    on_list = len(await shopping.active_entries(conn, household["id"], include_predicted=False))
    low = await inventory.stock_rows(conn, household["id"], statuses=["low", "out"])
    expiring = await inventory.stock_rows(conn, household["id"], expiring_within_days=7,
                                          today=local(now, timezone).date())
    if not (events or on_list or low or expiring):
        return None
    lines = _section("The week ahead:", [o.line(timezone) for o in events]) or ["Nothing booked this week."]
    if on_list:
        lines.append(f"Shopping list: {on_list} item{'' if on_list == 1 else 's'}.")
    if low:
        lines.append("Low or out: " + ", ".join(row["item"] for row in low) + ".")
    lines += _section("Use this week:", [f"{r['item']} ({r['location']}, {r['expires_on']:%-d %b})" for r in expiring])
    return "\n".join(lines)


async def weekly_digest(now: datetime | None = None) -> int:
    """Sunday 18:00 household time: the week ahead, the list, and what is low or expiring."""
    def run_key(here: datetime, household: dict[str, Any]) -> str | None:
        year, week, _ = here.isocalendar()
        return f"{year}-W{week:02d}" if here.weekday() == 6 and _due(here, WEEKLY_DIGEST_AT) else None

    return await _once_per_household("weekly_digest", now or utcnow(), run_key, _weekly_digest)


# ---------------------------------------------------------------- media retention
async def media_cleanup(store: MediaStore, retention_days: int, now: datetime | None = None, *,
                        limit: int = 200) -> int:
    """Delete stored media older than MEDIA_RETENTION_DAYS and forget where it was; captions
    and transcripts remain. Returns how many messages were cleaned. Safe to run twice: a
    cleaned message no longer matches."""
    now = now or utcnow()
    cleaned = 0
    async with tx() as conn:
        rows = await fetch_all(
            conn,
            """select id, household_id, media from messages
               where created_at < :cutoff
                 and jsonb_path_exists(media, '$[*] ? (@.storage_backend == $backend)',
                                       jsonb_build_object('backend', cast(:backend as text)))
               order by created_at for update skip locked limit :limit""",
            cutoff=now - timedelta(days=retention_days), backend=store.backend, limit=limit,
        )
        for row in rows:
            try:
                for ref in row["media"]:
                    if ref.get("storage_backend") == store.backend:
                        await store.delete(MediaRef.model_validate(ref))
            except Exception as exc:   # the store is unreachable: keep the refs and try again next run
                log.warning("media_cleanup_failed", household_id=row["household_id"], message_id=row["id"],
                            error=type(exc).__name__)
                continue
            await execute(conn, "update messages set media = cast(:media as jsonb) where id = :id",
                          media=jsonb(media_pipeline.without_storage(row["media"])), id=row["id"])
            cleaned += 1
    if cleaned:
        log.info("media_cleaned", messages=cleaned)
    return cleaned
