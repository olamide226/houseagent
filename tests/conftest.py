"""Test database setup. Tests run against local Postgres, only ever the `houseagent_test` database."""
import asyncio
import os

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

TEST_DATABASE = "houseagent_test"
TEST_URL = os.environ.get("TEST_DATABASE_URL", f"postgresql+asyncpg://localhost/{TEST_DATABASE}")

if make_url(TEST_URL).database != TEST_DATABASE:
    pytest.exit(f"refusing to run: tests only run against a database named {TEST_DATABASE}", returncode=2)

# The app reads its configuration from the environment; pin it before any app import.
os.environ.update(
    DATABASE_URL=TEST_URL,
    PUBLIC_BASE_URL="http://testserver",
    SESSION_SECRET="test-session-secret",
    SETUP_TOKEN="test-setup-token",
    LLM_PROVIDER="openai_compat",
    LLM_BASE_URL="http://llm.test/v1",
    LLM_API_KEY="test-key",
    LLM_MODEL="test-model",
    TG_BOT_TOKEN="424242:TEST-TOKEN",
    TG_BOT_USERNAME="home_test_bot",
    TG_WEBHOOK_SECRET="test-webhook-secret",
    WA_PHONE_NUMBER_ID="100000000000001",
    WA_ACCESS_TOKEN="test-access-token",
    WA_APP_SECRET="test-app-secret",
    WA_VERIFY_TOKEN="test-verify-token",
    BB_BASE_URL="http://mac-mini.test:1234",
    BB_PASSWORD="test-bb-password",
    BB_WEBHOOK_SECRET="test-bb-secret",
    DEBOUNCE_SECONDS="0",
)
for name in ("STT_PROVIDER", "STT_BASE_URL", "STT_MODEL", "WA_API_VERSION", "WA_REMINDER_TEMPLATE", "BB_PRIVATE_API"):
    os.environ.pop(name, None)

from app import db  # noqa: E402


async def _reset_schema() -> None:
    engine = create_async_engine(TEST_URL, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        name = (await conn.execute(text("select current_database()"))).scalar()
        if name != TEST_DATABASE:
            raise RuntimeError(f"refusing to reset database {name!r}")
        await conn.execute(text("drop schema public cascade"))
        await conn.execute(text("create schema public"))
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    """Rebuild the test schema from migration 0001 once per session."""
    from alembic import command
    from alembic.config import Config

    asyncio.run(_reset_schema())
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture(autouse=True)
async def _clean_tables(_schema: None) -> None:
    async with db.tx() as conn:
        await conn.execute(text("truncate households cascade"))


@pytest.fixture
async def client():
    """The api process, in memory. Sets up what the lifespan would."""
    import httpx

    from app.channels.base import build_adapters
    from app.config import get_settings
    from app.main import app
    from app.pipeline import inbound

    build_adapters(get_settings())
    inbound._invite_attempts.clear()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as http:
        yield http
