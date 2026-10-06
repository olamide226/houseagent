"""ChannelAdapter protocol and the adapter registry (spec section 5)."""
from typing import Protocol, runtime_checkable

from fastapi import Request

from app.config import Settings
from app.core.envelope import (
    Capabilities,
    Channel,
    DeliveryStatus,
    GroupUpdate,
    InboundEvent,
    MediaRef,
    SendResult,
)


class NotSupported(Exception):
    """The channel cannot do this; the router degrades (emoji as text, plain send for a template)."""


class ChannelError(Exception):
    """A send or fetch failed; the router retries with backoff."""


class PermanentError(ChannelError):
    """The channel refused for good (blocked, no such chat, bad token); the router does not retry."""


class ChannelAdapter(Protocol):
    channel: Channel
    capabilities: Capabilities

    async def verify(self, request: Request, body: bytes) -> None: ...   # raise HTTPException(401)
    async def parse(self, body: bytes) -> list[InboundEvent]: ...
    # What else a webhook carried: delivery statuses and the outcome of group creation.
    async def parse_updates(self, body: bytes) -> list[DeliveryStatus | GroupUpdate]: ...
    async def fetch_media(self, ref: MediaRef) -> tuple[bytes, str]: ...   # (bytes, mime)
    async def send_text(self, external_thread_id: str, text: str,
                        reply_to_external_id: str | None = None) -> SendResult: ...
    async def react(self, external_thread_id: str, external_message_id: str, emoji: str) -> None: ...
    async def send_template(self, external_thread_id: str, name: str, params: list[str]) -> SendResult: ...
    def dm_thread_id(self, handle: str) -> str: ...   # external thread id for a DM with this handle
    def format(self, text: str) -> str: ...          # *bold* etc. per platform


@runtime_checkable
class GroupHost(Protocol):
    """A channel whose API can create a group chat and hand out its invite link (WhatsApp)."""

    async def create_group(self, subject: str) -> str | None: ...   # the group's thread id, if known yet
    async def invite_link(self, external_thread_id: str) -> str: ...


@runtime_checkable
class HealthChecked(Protocol):
    """A channel that runs on a server of ours which can go away (BlueBubbles on the Mac)."""

    degraded: bool   # set by the worker's health check; the router then prefers people's other channels

    async def ping(self) -> bool: ...


def degraded(adapter: ChannelAdapter | None) -> bool:
    return isinstance(adapter, HealthChecked) and adapter.degraded


ADAPTERS: dict[Channel, ChannelAdapter] = {}


def build_adapters(settings: Settings) -> dict[Channel, ChannelAdapter]:
    """Register an adapter for each channel whose environment variables are present."""
    ADAPTERS.clear()
    if settings.telegram_enabled:
        from app.channels.telegram import TelegramAdapter

        assert settings.tg_bot_token and settings.tg_webhook_secret
        ADAPTERS[Channel.telegram] = TelegramAdapter(settings.tg_bot_token, settings.tg_webhook_secret)
    if settings.whatsapp_enabled:
        from app.channels.whatsapp import WhatsAppAdapter

        assert (settings.wa_phone_number_id and settings.wa_access_token and settings.wa_app_secret
                and settings.wa_verify_token)
        ADAPTERS[Channel.whatsapp] = WhatsAppAdapter(
            settings.wa_phone_number_id, settings.wa_access_token, settings.wa_app_secret,
            settings.wa_verify_token, api_version=settings.wa_api_version,
            reminder_template=settings.wa_reminder_template)
    if settings.imessage_enabled:
        from app.channels.imessage import IMessageAdapter

        assert settings.bb_base_url and settings.bb_password and settings.bb_webhook_secret
        ADAPTERS[Channel.imessage] = IMessageAdapter(
            settings.bb_base_url, settings.bb_password, settings.bb_webhook_secret,
            private_api=settings.bb_private_api)
    return ADAPTERS
