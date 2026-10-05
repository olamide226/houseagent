"""All configuration is environment variables read by one Settings class (spec section 3)."""
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
    agent_runtime: Literal["loop"] = "loop"
    default_timezone: str = "Europe/London"
    debounce_seconds: float = 4
    log_level: str = "INFO"

    llm_provider: Literal["openai_compat", "anthropic"]
    llm_base_url: str | None = None
    llm_api_key: str = ""
    llm_model: str
    llm_supports_images: bool = True
    llm_max_tool_iterations: int = 8

    stt_provider: Literal["openai_compat"] | None = None
    stt_base_url: str | None = None
    stt_api_key: str = ""
    stt_model: str | None = None

    tg_bot_token: str | None = None
    tg_bot_username: str | None = None
    tg_webhook_secret: str | None = None

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.tg_bot_token and self.tg_webhook_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
