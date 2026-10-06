"""iMessage adapter over a BlueBubbles server (spec section 6.3).

Payload shapes and paths follow the BlueBubbles server source at v1.9.9 as read on 6 Oct 2026: a
webhook body is `{"type": ..., "data": ...}` with the message in its notification form, every
call carries the server password as a query parameter, and a tapback is one of six fixed kinds.
See docs/channels.md and ADR 0027.
"""
import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

import httpx
from fastapi import HTTPException, Request

from app.channels.base import ChannelError, NotSupported, PermanentError
from app.core.envelope import (
    Capabilities,
    Channel,
    DeliveryStatus,
    GroupUpdate,
    InboundEvent,
    MediaRef,
    SendResult,
)

TAPBACKS = {"love": "❤️", "like": "\U0001F44D", "dislike": "\U0001F44E", "laugh": "\U0001F602",
            "emphasize": "‼️", "question": "❓"}
# What we can send as a tapback: the six kinds, and the ack tick, which iMessage has no tapback for.
REACTIONS = {**{emoji: kind for kind, emoji in TAPBACKS.items()}, "✅": "like"}
ATTACHMENT_MARK = "￼"  # where an attachment sits in a message's text
CAF = "audio/x-caf"
MIME_NAMES = {"audio/mp3": "audio/mpeg"}   # the server's name for the MP3 it makes of a voice note


class IMessageAdapter:
    channel = Channel.imessage

    def __init__(self, base_url: str, password: str, webhook_secret: str, *, private_api: bool = False) -> None:
        self._api = base_url.rstrip("/") + "/api/v1"
        self._auth = {"password": password}
        self._secret = webhook_secret
        self._private_api = private_api
        self.degraded = False   # set by the worker's health check; the router then prefers other channels
        self.capabilities = Capabilities(
            groups=True, reactions=private_api, ack_emoji="✅", voice_in=True, images_in=True,
            threaded_replies=private_api, proactive_window_hours=None, max_text_len=10000, formatting="plain",
        )

    async def verify(self, request: Request, body: bytes) -> None:
        # BlueBubbles does not sign what it sends: the secret is in the URL it was given.
        given = request.query_params.get("secret", "")
        if not hmac.compare_digest(given.encode(), self._secret.encode()):
            raise HTTPException(status_code=401)

    async def parse(self, body: bytes) -> list[InboundEvent]:
        payload = json.loads(body)
        if payload.get("type") != "new-message":
            return []
        event = self._message(payload, payload.get("data") or {})
        return [event] if event else []

    async def parse_updates(self, body: bytes) -> list[DeliveryStatus | GroupUpdate]:
        payload = json.loads(body)
        data = payload.get("data") or {}
        if payload.get("type") != "message-send-error" or not data.get("guid"):
            return []
        return [DeliveryStatus(channel=Channel.imessage, external_message_id=data["guid"], status="failed",
                               error=f"iMessage error {data.get('error', '')}".strip())]

    def _message(self, payload: dict[str, Any], data: dict[str, Any]) -> InboundEvent | None:
        handle = ((data.get("handle") or {}).get("address") or "").strip()
        if data.get("isFromMe") or not handle or not data.get("guid"):
            return None   # our own send echoed back, or nobody
        chat = (data.get("chats") or [{}])[0]
        group = ";+;" in chat.get("guid", "")   # iMessage;+;chat... is a group, iMessage;-;<handle> a DM
        text = emoji = target = None
        media: list[MediaRef] = []
        if data.get("associatedMessageType"):
            # A tapback is a message about another message; its text ("Liked ...") is not read.
            emoji = TAPBACKS.get(data["associatedMessageType"])
            target = (data.get("associatedMessageGuid") or "").rpartition("/")[2].removeprefix("bp:")
            if not (emoji and target):
                return None   # a tapback taken back, a sticker, or a kind this does not know
        else:
            text = (data.get("text") or "").replace(ATTACHMENT_MARK, "").strip() or None
            media = [MediaRef(kind=_kind(a), mime=a.get("mimeType") or (CAF if _is_caf(a) else None),
                              external_id=a["guid"]) for a in data.get("attachments") or [] if a.get("guid")]
            if not (text or media):
                return None   # group renames, people joining or leaving
        return InboundEvent(
            channel=Channel.imessage,
            external_message_id=data["guid"],
            # A DM is always the chat with its sender, whatever service prefix the server reports.
            external_thread_id=chat["guid"] if group else self.dm_thread_id(handle),
            scope="group" if group else "dm",
            sender_handle=handle,
            text=text,
            media=media,
            reply_to_external_id=data.get("threadOriginatorGuid"),
            reaction_emoji=emoji,
            reaction_target_external_id=target,
            sent_at=datetime.fromtimestamp((data.get("dateCreated") or 0) / 1000, UTC),
            raw=payload,
        )

    async def fetch_media(self, ref: MediaRef) -> tuple[bytes, str]:
        # The server hands HEIC photos back as JPEG, and voice notes as MP3 when it can convert them.
        response = await self._request("GET", f"attachment/{quote(ref.external_id or '', safe='')}/download",
                                       what="attachment download", seconds=60)
        if response.content.startswith(b"caff"):
            return response.content, CAF   # an unconverted voice note; the pipeline converts it
        mime = response.headers.get("content-type", "").split(";")[0].strip() or ref.mime
        return response.content, MIME_NAMES.get(mime or "", mime) or "application/octet-stream"

    async def send_text(self, external_thread_id: str, text: str,
                        reply_to_external_id: str | None = None) -> SendResult:
        payload = {"chatGuid": external_thread_id, "message": text, "tempGuid": str(uuid.uuid4()),
                   "method": "private-api" if self._private_api else "apple-script"}
        if reply_to_external_id and self._private_api:
            payload["selectedMessageGuid"] = reply_to_external_id   # a threaded reply
        sent = await self._request("POST", "message/text", what="send", payload=payload, seconds=60)
        return SendResult(external_id=_data(sent).get("guid"))

    async def react(self, external_thread_id: str, external_message_id: str, emoji: str) -> None:
        if not self._private_api or emoji not in REACTIONS:
            raise NotSupported("tapbacks need BB_PRIVATE_API, and there are only six of them")
        await self._request("POST", "message/react", what="tapback", payload={
            "chatGuid": external_thread_id, "selectedMessageGuid": external_message_id,
            "reaction": REACTIONS[emoji], "partIndex": 0})

    async def send_template(self, external_thread_id: str, name: str, params: list[str]) -> SendResult:
        raise NotSupported("imessage has no message templates")

    def dm_thread_id(self, handle: str) -> str:
        return f"iMessage;-;{handle}"

    def format(self, text: str) -> str:
        return text

    async def ping(self) -> bool:
        """Whether the BlueBubbles server answers. Never raises."""
        try:
            return bool(_data(await self._request("GET", "ping", what="ping", seconds=10)) == "pong")
        except ChannelError:
            return False

    async def _request(self, method: str, path: str, *, what: str, payload: dict[str, Any] | None = None,
                       seconds: float = 20) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=seconds) as client:
                response = await client.request(method, f"{self._api}/{path}", params=self._auth, json=payload)
        except httpx.HTTPError as exc:
            # Never include the exception text: httpx errors can carry the URL, which holds the password.
            raise ChannelError(f"imessage {what} failed: {type(exc).__name__}") from None
        if response.status_code >= 400:
            # A refused request (bad chat, wrong password) is final; a 5xx is the Mac or iMessage having trouble.
            final = response.status_code < 500
            raise (PermanentError if final else ChannelError)(
                f"imessage {what} failed: HTTP {response.status_code} {_error(response)}".rstrip())
        return response


def _data(response: httpx.Response) -> Any:
    try:
        body = response.json()
    except ValueError:
        raise ChannelError("imessage answered with something that is not JSON") from None
    return body.get("data") if isinstance(body, dict) else None


def _error(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["message"])[:200]
    except (ValueError, KeyError, TypeError):
        return ""


def _is_caf(attachment: dict[str, Any]) -> bool:
    return (attachment.get("uti") == "com.apple.coreaudio-format"
            or (attachment.get("transferName") or "").lower().endswith(".caf"))


def _kind(attachment: dict[str, Any]) -> Literal["image", "audio", "video", "document", "location"]:
    mime = attachment.get("mimeType") or ""
    if mime == "text/x-vlocation" or (attachment.get("transferName") or "").endswith(".loc.vcf"):
        return "location"   # a shared location is a small card file; the coordinates are inside it
    if mime.startswith("audio/") or _is_caf(attachment):
        return "audio"
    if mime.startswith("image/"):
        return "image"
    return "video" if mime.startswith("video/") else "document"
