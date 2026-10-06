"""Worker process: an asyncio supervisor that runs each job as its own task.

    python -m app.worker.main
"""
import asyncio
import signal
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

import structlog
import uvicorn
from fastapi import FastAPI

from app.agent import internal
from app.agent.runtime import make_runtime
from app.channels.base import HealthChecked, build_adapters
from app.config import configure_logging, get_settings
from app.core.timeutil import utcnow
from app.db import engine, tx
from app.llm.base import make_llm
from app.llm.stt import make_stt
from app.media.store import make_media_store
from app.services import households
from app.worker import jobs

log = structlog.get_logger()
RESTART_SECONDS = 5
HEARTBEAT_SECONDS = 60
SCHEDULED: dict[str, tuple[Callable[[], Awaitable[int]], float]] = {
    "fire_reminders": (jobs.fire_reminders, jobs.REMINDERS_SECONDS),
    "expand_recurrence": (jobs.expand_recurrence, jobs.RECURRENCE_SECONDS),
    "daily_brief": (jobs.daily_brief, jobs.DIGEST_POLL_SECONDS),
    "weekly_digest": (jobs.weekly_digest, jobs.DIGEST_POLL_SECONDS),
    "low_stock_prompt": (jobs.low_stock_prompt, jobs.DIGEST_POLL_SECONDS),
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


async def heartbeat(path: Path) -> None:
    """Every minute: touch the file the liveness probe watches, and tell the database, which is
    where the dashboard's System page looks. A database that is away must not stop the file."""
    while True:
        log.info("worker_heartbeat")
        await asyncio.to_thread(path.touch)
        try:
            async with tx() as conn:
                await households.beat(conn, utcnow())
        except Exception as exc:
            log.warning("worker_heartbeat_not_recorded", error=type(exc).__name__)
        await asyncio.sleep(HEARTBEAT_SECONDS)


async def serve_internal(port: int) -> None:
    """The tool bridge for turns this process runs under the Letta runtime (app/agent/internal.py)."""
    bridge = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    bridge.include_router(internal.router)
    # No lifespan: the bridge has nothing to start or stop, and a cancelled lifespan logs a traceback.
    await uvicorn.Server(uvicorn.Config(bridge, host="0.0.0.0", port=port, log_config=None, lifespan="off")).serve()


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    adapters = build_adapters(settings)
    media = make_media_store(settings)
    runtime = make_runtime(settings, media, tool_url=settings.worker_internal_url)
    stt = make_stt(settings)
    outbox_wake = asyncio.Event()
    scheduled = dict(SCHEDULED)
    scheduled["consumption_model"] = (partial(jobs.consumption_model, make_llm(settings, fast=True)),
                                      jobs.DIGEST_POLL_SECONDS)
    if media is not None:
        scheduled["media_cleanup"] = (partial(jobs.media_cleanup, media, settings.media_retention_days),
                                      jobs.MEDIA_CLEANUP_SECONDS)
    for adapter in adapters.values():
        if isinstance(adapter, HealthChecked):
            scheduled[f"{adapter.channel.value}_health"] = (partial(jobs.imessage_health, adapter),
                                                           jobs.IMESSAGE_HEALTH_SECONDS)
    tasks = [
        asyncio.create_task(supervise(
            "inbound", lambda: jobs.inbound_job(settings, runtime, adapters, stt, media, outbox_wake))),
        asyncio.create_task(supervise("outbox", lambda: jobs.outbox_job(adapters, outbox_wake))),
        *(asyncio.create_task(supervise(name, partial(jobs.every, seconds, job, outbox_wake)))
          for name, (job, seconds) in scheduled.items()),
        asyncio.create_task(heartbeat(Path(settings.worker_heartbeat_file))),
    ]
    if settings.agent_runtime == "letta":
        port = urlsplit(settings.worker_internal_url).port or 8001
        tasks.append(asyncio.create_task(supervise("internal_tools", partial(serve_internal, port))))
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
