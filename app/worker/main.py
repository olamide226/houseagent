"""Worker process: an asyncio supervisor that runs each job as its own task.

    python -m app.worker.main
"""
import asyncio
import signal
from collections.abc import Awaitable, Callable
from functools import partial

import structlog

from app.agent.loop import LoopRuntime
from app.channels.base import build_adapters
from app.config import configure_logging, get_settings
from app.db import engine
from app.llm.base import make_llm
from app.llm.stt import make_stt
from app.media.store import make_media_store
from app.worker import jobs

log = structlog.get_logger()
RESTART_SECONDS = 5
HEARTBEAT_SECONDS = 60
SCHEDULED: dict[str, tuple[Callable[[], Awaitable[int]], float]] = {
    "reminders": (jobs.fire_reminders, jobs.REMINDERS_SECONDS),
    "recurrence": (jobs.expand_recurrence, jobs.RECURRENCE_SECONDS),
    "daily_brief": (jobs.daily_brief, jobs.DIGEST_POLL_SECONDS),
    "weekly_digest": (jobs.weekly_digest, jobs.DIGEST_POLL_SECONDS),
}


async def supervise(name: str, job: Callable[[], Awaitable[None]]) -> None:
    """Run a job forever, restarting it 5 s after a crash."""
    while True:
        try:
            await job()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("job_crashed", job=name)
        await asyncio.sleep(RESTART_SECONDS)


async def heartbeat() -> None:
    while True:
        log.info("worker_heartbeat")
        await asyncio.sleep(HEARTBEAT_SECONDS)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    adapters = build_adapters(settings)
    media = make_media_store(settings)
    runtime = LoopRuntime(make_llm(settings), media=media, agent_name=settings.agent_name,
                          max_iterations=settings.llm_max_tool_iterations)
    stt = make_stt(settings)
    outbox_wake = asyncio.Event()
    scheduled = dict(SCHEDULED)
    if media is not None:
        scheduled["media_cleanup"] = (partial(jobs.media_cleanup, media, settings.media_retention_days),
                                      jobs.MEDIA_CLEANUP_SECONDS)
    tasks = [
        asyncio.create_task(supervise(
            "inbound", lambda: jobs.inbound_job(settings, runtime, adapters, stt, media, outbox_wake))),
        asyncio.create_task(supervise("outbox", lambda: jobs.outbox_job(adapters, outbox_wake))),
        *(asyncio.create_task(supervise(name, partial(jobs.every, seconds, job, outbox_wake)))
          for name, (job, seconds) in scheduled.items()),
        asyncio.create_task(heartbeat()),
    ]
    log.info("worker_started", channels=[c.value for c in adapters], llm_provider=settings.llm_provider)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await stop.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
