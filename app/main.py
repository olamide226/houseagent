"""API process: receives webhooks, serves the dashboard, returns 200 fast.

    uvicorn app.main:app
"""
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import DBAPIError, InterfaceError

from app.agent import internal
from app.agent.runtime import make_runtime
from app.channels.base import ADAPTERS, build_adapters
from app.channels.whatsapp import WhatsAppAdapter
from app.config import configure_logging, get_settings
from app.core.envelope import Channel
from app.dashboard import auth, routes
from app.db import engine, fetch_val, tx
from app.ics import routes as ics
from app.media.store import make_media_store
from app.pipeline import inbound
from app.presence import routes as presence

log = structlog.get_logger()
MIGRATION_HEAD = "0001"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level)
    build_adapters(settings)
    # The Playground runs turns in the api process; chat turns run in the worker.
    app.state.runtime = make_runtime(settings, make_media_store(settings))
    yield
    await engine().dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="Household Agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "dashboard" / "static"), name="static")
    app.include_router(auth.router)
    app.include_router(routes.router)
    app.include_router(ics.router)
    app.include_router(presence.router)
    app.include_router(internal.router)   # answers 404 unless INTERNAL_TOOL_TOKEN is set; not on the public ingress

    @app.exception_handler(auth.LoginRequired)
    async def login_required(request: Request, exc: auth.LoginRequired) -> Response:
        return auth.templates.TemplateResponse(request, "message.html", {
            "title": "Log in from chat",
            "message": f"Send “dashboard” to {get_settings().agent_name} to get a one-time login link.",
        }, status_code=401)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        """Database reachable and migrations current."""
        try:
            async with tx() as conn:
                version = await fetch_val(conn, "select version_num from alembic_version")
        except Exception:
            raise HTTPException(status_code=503, detail="database unavailable") from None
        if version != MIGRATION_HEAD:
            raise HTTPException(status_code=503, detail="migrations pending")
        return {"status": "ready"}

    @app.get("/webhooks/whatsapp", response_class=PlainTextResponse)
    async def whatsapp_subscription(request: Request) -> str:
        """Meta's check when the webhook is registered: echo `hub.challenge` if the verify token is ours."""
        adapter = ADAPTERS.get(Channel.whatsapp)
        if not isinstance(adapter, WhatsAppAdapter):
            raise HTTPException(status_code=404)
        query = request.query_params
        return adapter.subscription_challenge(query.get("hub.mode", ""), query.get("hub.verify_token", ""),
                                              query.get("hub.challenge", ""))

    @app.post("/webhooks/{channel}")
    async def webhook(channel: Channel, request: Request) -> Response:
        """Verify, parse, persist, return 200. All slow work happens in the worker (spec 7.1)."""
        adapter = ADAPTERS.get(channel)
        if adapter is None:
            raise HTTPException(status_code=404)
        body = await request.body()
        await adapter.verify(request, body)
        try:
            await inbound.receive(adapter, body, ADAPTERS)
        except (DBAPIError, InterfaceError, OSError):
            log.exception("webhook_database_error", channel=channel.value)
            raise HTTPException(status_code=503) from None   # the provider retries
        return Response(status_code=200)

    return app


app = create_app()
