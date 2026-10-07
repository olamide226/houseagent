"""All configuration is environment variables read by one Settings class (spec section 3)."""
import logging
import re
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    public_base_url: str
    session_secret: str
    setup_token: str | None = None

    agent_name: str = "Home"
    # The iCloud link of the Shortcut the household's admin shared once (docs/presence.md). With it,
    # nobody else builds anything on their phone: they add that Shortcut and paste a link.
    presence_shortcut_url: str | None = None
    agent_runtime: Literal["loop", "letta"] = "loop"
    default_timezone: str = "Europe/London"
    debounce_seconds: float = 4
    log_level: str = "INFO"
    worker_heartbeat_file: str = "/tmp/worker-heartbeat"   # touched every minute; the worker's liveness probe
    eval_results_dir: str = "tests/evals/.results"         # where the eval suite leaves its last result

    llm_provider: Literal["openai_compat", "anthropic", "claude_code", "codex_cli"]
    llm_base_url: str | None = None
    llm_api_key: str = ""
    llm_model: str
    llm_fast_model: str | None = None
    llm_supports_images: bool = True
    llm_max_tool_iterations: int = 8
    llm_cli_path: str | None = None           # claude_code and codex_cli: the CLI, when it is not on PATH
    llm_cli_timeout: float = 120              # seconds one model step may take before its CLI is stopped

    stt_provider: Literal["openai_compat"] | None = None
    stt_base_url: str | None = None
    stt_api_key: str = ""
    stt_model: str | None = None

    media_backend: Literal["s3", "imgbb"] = "s3"
    s3_endpoint: str | None = None
    s3_bucket: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_region: str = "us-east-1"
    imgbb_api_key: str | None = None
    media_retention_days: int = 90

    tg_bot_token: str | None = None
    tg_bot_username: str | None = None
    tg_webhook_secret: str | None = None

    wa_phone_number_id: str | None = None
    wa_access_token: str | None = None
    wa_app_secret: str | None = None
    wa_verify_token: str | None = None
    wa_api_version: str = "v26.0"
    wa_reminder_template: str = "household_reminder"

    bb_base_url: str | None = None
    bb_password: str | None = None
    bb_webhook_secret: str | None = None
    bb_private_api: bool = False

    # The optional Letta runtime (spec 8.4). Its tools call back into `/internal/tools/*`.
    letta_base_url: str | None = None
    letta_api_key: str | None = None
    letta_model: str | None = None            # the model's handle as the Letta server names it
    internal_tool_token: str | None = None
    internal_base_url: str | None = None      # where Letta reaches the api's /internal; default PUBLIC_BASE_URL
    worker_internal_url: str = "http://localhost:8001"   # where Letta reaches the worker's; it listens on this port

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.tg_bot_token and self.tg_webhook_secret)

    @property
    def whatsapp_enabled(self) -> bool:
        return bool(self.wa_phone_number_id and self.wa_access_token and self.wa_app_secret
                    and self.wa_verify_token)

    @property
    def imessage_enabled(self) -> bool:
        return bool(self.bb_base_url and self.bb_password and self.bb_webhook_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()


# A presence, login or calendar token is the last part of its path, and the setup token and the
# iMessage webhook secret are query parameters. None of them belongs in a log.
_SECRET_IN_URL = re.compile(r"(^/(?:presence|login|ics)/|[?&](?:token|secret)=)[^/?&\s]+")


def mask_secrets(url: str) -> str:
    return _SECRET_IN_URL.sub(r"\1…", url)


class _MaskAccessLog(logging.Filter):
    """uvicorn's access log prints each request's path and query: mask the secrets in it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(mask_secrets(arg) if isinstance(arg, str) else arg for arg in record.args)
        return True


def configure_logging(level: str) -> None:
    """JSON logs via structlog. Lines carry ids, never message text or tokens."""
    import structlog

    logging.basicConfig(level=level.upper(), format="%(message)s")
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(existing, _MaskAccessLog) for existing in access.filters):
        access.addFilter(_MaskAccessLog())
    # httpx logs full request URLs at INFO, and the Telegram URL contains the bot token.
    for noisy in ("httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
    )
