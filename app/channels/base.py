"""ChannelAdapter protocol and the adapter registry (spec section 5)."""
from typing import Protocol

from fastapi import Request

from app.config import Settings
from app.core.envelope import Capabilities, Channel, InboundEvent, MediaRef, SendResult


class NotSupported(Exception):
    """The channel cannot do this; the router degrades (emoji as text, plain send for a template)."""


class ChannelError(Exception):
    """A send or fetch failed; the router retries with backoff."""


class ChannelAdapter(Protocol):
    channel: Channel
    capabilities: Capabilities

    async def verify(self, request: Request, body: bytes) -> None: ...   # raise HTTPException(401)
    async def parse(self, body: bytes) -> list[InboundEvent]: ...
    async def fetch_media(self, ref: MediaRef) -> tuple[bytes, str]: ...   # (bytes, mime)
    async def send_text(self, external_thread_id: str, text: str,
                        reply_to_external_id: str | None = None) -> SendResult: ...
    async def react(self, external_thread_id: str, external_message_id: str, emoji: str) -> None: ...
    async def send_template(self, external_thread_id: str, name: str, params: list[str]) -> SendResult: ...
    def dm_thread_id(self, handle: str) -> str: ...   # external thread id for a DM with this handle
    def format(self, text: str) -> str: ...          # *bold* etc. per platform


ADAPTERS: dict[Channel, ChannelAdapter] = {}


def build_adapters(settings: Settings) -> dict[Channel, ChannelAdapter]:
    """Register an adapter for each channel whose environment variables are present."""
    ADAPTERS.clear()
    if settings.telegram_enabled:
        from app.channels.telegram import TelegramAdapter

        assert settings.tg_bot_token and settings.tg_webhook_secret
        ADAPTERS[Channel.telegram] = TelegramAdapter(settings.tg_bot_token, settings.tg_webhook_secret)
    return ADAPTERS
