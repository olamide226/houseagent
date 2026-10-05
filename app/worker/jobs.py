"""Worker jobs. Milestone 1 runs two: inbound processing and outbox dispatch (spec section 10)."""
import asyncio
import contextlib

import structlog

from app.agent.base import AgentRuntime
from app.channels.base import ChannelAdapter
from app.config import Settings
from app.core.envelope import Channel
from app.db import engine, tx
from app.llm.stt import SpeechToText
from app.pipeline import inbound, router

log = structlog.get_logger()
POLL_SECONDS = 2.0


async def inbound_job(settings: Settings, runtime: AgentRuntime, adapters: dict[Channel, ChannelAdapter],
                      stt: SpeechToText | None, outbox_wake: asyncio.Event) -> None:
    """Process settled batches. Wakes on NOTIFY inbound, when a batch's debounce ends, or every 2 s."""
    wake = asyncio.Event()
    listener = await engine().raw_connection()
    try:
        await listener.driver_connection.add_listener("inbound", lambda *_: wake.set())  # type: ignore[union-attr]
        while True:
            wake.clear()
            async with tx() as conn:
                ready = await inbound.ready_households(conn, settings.debounce_seconds)
            for household_id in ready:
                await inbound.process_household(household_id, runtime, adapters, stt=stt,
                                                public_base_url=settings.public_base_url)
                outbox_wake.set()
            async with tx() as conn:
                pending = await inbound.seconds_until_ready(conn, settings.debounce_seconds)
            timeout = POLL_SECONDS if pending is None else min(POLL_SECONDS, pending)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout)
    finally:
        listener.close()


async def outbox_job(adapters: dict[Channel, ChannelAdapter], wake: asyncio.Event) -> None:
    """Dispatch due sends every 2 s, or at once when a turn has just queued a reply."""
    while True:
        wake.clear()
        while await router.dispatch_due(adapters):
            pass
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), POLL_SECONDS)
