"""The three types that carry every message (spec section 5).

Adapters produce InboundEvent, the pipeline turns it into an Envelope for the agent, and
everything outbound is an OutboundMessage row in `outbox`.
"""
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel


class Channel(StrEnum):
    telegram = "telegram"
    whatsapp = "whatsapp"
    imessage = "imessage"


class MediaRef(BaseModel):
    kind: Literal["image", "audio", "video", "document", "location"]
    mime: str | None = None
    external_id: str | None = None   # provider media id / file id / attachment guid
    storage_backend: Literal["s3", "imgbb"] | None = None
    storage_key: str | None = None   # s3: {household}/{message_id}/{n}.{ext}; imgbb: image id
    storage_url: str | None = None   # imgbb only: direct URL (public to anyone with the link)
    delete_url: str | None = None    # imgbb only: deletion link for early cleanup
    transcript: str | None = None    # audio only, set by media.py
    lat: float | None = None
    lng: float | None = None
    caption: str | None = None


class InboundEvent(BaseModel):
    """Adapter output. Channel-shaped facts only, no household knowledge."""
    channel: Channel
    external_message_id: str
    external_thread_id: str
    scope: Literal["dm", "group"]
    sender_handle: str               # normalised: E.164 phone, Telegram user id, Apple ID
    sender_name: str | None = None
    text: str | None = None
    media: list[MediaRef] = []
    reply_to_external_id: str | None = None
    reaction_emoji: str | None = None        # set when the event IS a reaction
    reaction_target_external_id: str | None = None
    sent_at: datetime
    raw: dict[str, Any]


class Envelope(BaseModel):
    """What the agent sees. Built after identity resolution and debounce."""
    household_id: str
    member_id: str | None            # None for system turns (jobs, presence)
    member_name: str | None
    thread_id: str | None
    message_ids: list[str]           # debounced batch, oldest first
    channel: Channel | None          # None for the dashboard Playground
    scope: Literal["dm", "group"] | None
    text: str                        # joined texts + "[voice note] ..." transcripts + reaction lines
    images: list[MediaRef] = []
    reply_to_text: str | None = None
    kind: Literal["user", "system"] = "user"
    received_at: datetime


class OutboundMessage(BaseModel):
    household_id: str
    target: Literal["thread", "member", "household"]
    thread_id: str | None = None
    member_id: str | None = None
    text: str | None = None
    react_emoji: str | None = None           # "ack" means the adapter's ack emoji
    reply_to_message_id: str | None = None
    urgency: Literal["low", "normal", "high"] = "normal"
    respect_quiet_hours: bool = True
    dedupe_key: str | None = None


class Capabilities(BaseModel):
    groups: bool
    reactions: bool
    ack_emoji: str                   # Telegram: "\U0001F44D", WhatsApp/iMessage: "✅"
    voice_in: bool
    images_in: bool
    threaded_replies: bool
    proactive_window_hours: int | None   # 24 for WhatsApp, None = unlimited
    max_text_len: int                    # 4096 Telegram/WhatsApp, 10000 iMessage
    formatting: Literal["plain", "whatsapp", "telegram_html"]


class SendResult(BaseModel):
    external_id: str | None
