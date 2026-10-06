"""WhatsApp Cloud API adapter (spec section 6.2).

Payload shapes follow Meta's reference as read on 6 Oct 2026 (Graph API v26.0), which differs
from the spec in two ways: a sender is identified by a business-scoped user id (`from_user_id`),
because the phone number can be absent, and a group is created asynchronously, its id arriving in
a `group_lifecycle_update` webhook. See docs/channels.md and ADRs 0019 and 0021.
"""
import hashlib
import hmac
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import HTTPException, Request

from app.channels.base import ChannelError, PermanentError
from app.core.envelope import (
    Capabilities,
    Channel,
    DeliveryStatus,
    GroupUpdate,
    InboundEvent,
    MediaRef,
    SendResult,
)

GRAPH = "https://graph.facebook.com"
TEMPLATE_LANGUAGE = "en_GB"
# Graph error codes that mean "slow down" whatever the HTTP status; any other 4xx is final.
THROTTLED = {4, 80007, 130429, 131048, 131056}
STATUSES = {"sent", "delivered", "read", "failed"}
_PHONE = re.compile(r"\+\d+")
_USER_ID = re.compile(r"[A-Z]{2,3}\.[A-Za-z0-9]+")   # business-scoped user id, e.g. GB.13491208655302741918


class WhatsAppAdapter:
    channel = Channel.whatsapp

    def __init__(self, phone_number_id: str, access_token: str, app_secret: str, verify_token: str, *,
                 api_version: str = "v26.0", reminder_template: str | None = None) -> None:
        self._phone_number_id = phone_number_id
        self._auth = {"Authorization": f"Bearer {access_token}"}
        self._app_secret = app_secret
        self._verify_token = verify_token
        self._base = f"{GRAPH}/{api_version}"
        self.capabilities = Capabilities(
            groups=True, reactions=True, ack_emoji="✅", voice_in=True, images_in=True,
            threaded_replies=True, proactive_window_hours=24, proactive_template=reminder_template,
            max_text_len=4096, formatting="whatsapp",
        )

    def subscription_challenge(self, mode: str, token: str, challenge: str) -> str:
        """Meta's subscription check (GET): the challenge to echo, if the verify token is ours."""
        if mode != "subscribe" or not hmac.compare_digest(token.encode(), self._verify_token.encode()):
            raise HTTPException(status_code=403)
        return challenge

    async def verify(self, request: Request, body: bytes) -> None:
        given = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(self._app_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(given.encode(), expected.encode()):
            raise HTTPException(status_code=401)

    async def parse(self, body: bytes) -> list[InboundEvent]:
        payload = json.loads(body)
        events: list[InboundEvent] = []
        for value in _values(payload, "messages"):
            names = {key: contact.get("profile", {}).get("name") for contact in value.get("contacts", [])
                     for key in (contact.get("user_id"), contact.get("wa_id")) if key}
            own = _digits(value.get("metadata", {}).get("display_phone_number"))
            for message in value.get("messages", []):
                event = _message(payload, message, names, own)
                if event:
                    events.append(event)
        return events

    async def parse_updates(self, body: bytes) -> list[DeliveryStatus | GroupUpdate]:
        payload = json.loads(body)
        updates: list[DeliveryStatus | GroupUpdate] = []
        for value in _values(payload, "messages"):
            updates += [
                DeliveryStatus(channel=Channel.whatsapp, external_message_id=status["id"], status=status["status"],
                               error=_error(status.get("errors")))
                for status in value.get("statuses", []) if status.get("status") in STATUSES
            ]
        for value in _values(payload, "group_lifecycle_update"):
            for group in value.get("groups", []):
                if group.get("type") != "group_create":
                    continue
                error = _error(group.get("errors"))
                updates.append(GroupUpdate(
                    channel=Channel.whatsapp, subject=group.get("subject", ""), error=error,
                    external_thread_id=None if error else group.get("group_id")))
        return updates

    async def fetch_media(self, ref: MediaRef) -> tuple[bytes, str]:
        info = await self._graph("GET", quote(ref.external_id or "", safe=""), what="media lookup")
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.get(info["url"], headers=self._auth)   # the link lives five minutes
        except httpx.HTTPError as exc:
            raise ChannelError(f"whatsapp media download failed: {type(exc).__name__}") from None
        if response.status_code != 200:
            raise ChannelError(f"whatsapp media download failed: HTTP {response.status_code}")
        return response.content, _mime(info.get("mime_type")) or ref.mime or "application/octet-stream"

    async def send_text(self, external_thread_id: str, text: str,
                        reply_to_external_id: str | None = None) -> SendResult:
        payload: dict[str, Any] = {**_recipient(external_thread_id), "type": "text", "text": {"body": text}}
        if reply_to_external_id:
            payload["context"] = {"message_id": reply_to_external_id}
        return await self._send(payload)

    async def react(self, external_thread_id: str, external_message_id: str, emoji: str) -> None:
        await self._send({**_recipient(external_thread_id), "type": "reaction",
                          "reaction": {"message_id": external_message_id, "emoji": emoji}})

    async def send_template(self, external_thread_id: str, name: str, params: list[str]) -> SendResult:
        # A template parameter may not hold a line break, a tab or a run of spaces.
        parameters = [{"type": "text", "text": " ".join(param.split())} for param in params]
        return await self._send({**_recipient(external_thread_id), "type": "template", "template": {
            "name": name, "language": {"code": TEMPLATE_LANGUAGE},
            "components": [{"type": "body", "parameters": parameters}],
        }})

    async def create_group(self, subject: str) -> str | None:
        """Ask for a group. Creation is asynchronous and the reply is undocumented: the id normally
        comes later, in a `group_lifecycle_update` webhook, so None is the usual answer."""
        created = await self._graph("POST", f"{self._phone_number_id}/groups", what="group creation",
                                    payload={"messaging_product": "whatsapp", "subject": subject})
        return str(created["id"]) if created.get("id") else None

    async def invite_link(self, external_thread_id: str) -> str:
        found = await self._graph("GET", f"{quote(external_thread_id, safe='')}/invite_link", what="invite link")
        return str(found["invite_link"])

    def dm_thread_id(self, handle: str) -> str:
        return handle   # a DM is addressed by the person's own id

    def format(self, text: str) -> str:
        return text   # the agent writes plain text, which WhatsApp shows as it is

    async def _send(self, payload: dict[str, Any]) -> SendResult:
        sent = await self._graph("POST", f"{self._phone_number_id}/messages", what="send", payload=payload)
        return SendResult(external_id=sent["messages"][0]["id"])

    async def _graph(self, method: str, path: str, *, what: str,
                     payload: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.request(method, f"{self._base}/{path}", json=payload, headers=self._auth)
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ChannelError(f"whatsapp {what} failed: {type(exc).__name__}") from None
        error = data.get("error") if isinstance(data, dict) else None
        if response.status_code >= 400 or error or not isinstance(data, dict):
            code = (error or {}).get("code", response.status_code)
            final = 400 <= response.status_code < 500 and response.status_code != 429 and code not in THROTTLED
            raise (PermanentError if final else ChannelError)(
                f"whatsapp {what} failed: {code} {(error or {}).get('message', '')}".rstrip())
        return data


def _values(payload: dict[str, Any], field: str) -> Iterator[dict[str, Any]]:
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            if change.get("field") == field:
                yield change.get("value", {})


def _message(payload: dict[str, Any], message: dict[str, Any], names: dict[str, str | None],
             own: str) -> InboundEvent | None:
    handle = _handle(message)
    if handle is None or (own and _digits(message.get("from")) == own):
        return None   # nobody, or the business number itself
    kind = message.get("type")
    text = emoji = target = None
    media: list[MediaRef] = []
    if kind == "text":
        text = message["text"]["body"]
    elif kind == "interactive":
        text = message["interactive"].get("button_reply", {}).get("title")
    elif kind in ("audio", "image", "document"):
        part = message[kind]
        media.append(MediaRef(kind=kind, mime=_mime(part.get("mime_type")), external_id=part["id"]))
        text = part.get("caption")   # in the text, as Telegram's captions are: one shape for the agent
    elif kind == "location":
        media.append(MediaRef(kind="location", lat=message["location"]["latitude"],
                              lng=message["location"]["longitude"]))
    elif kind == "reaction":
        emoji, target = message["reaction"].get("emoji"), message["reaction"]["message_id"]
    if not (text or media or emoji):
        return None   # stickers, contact cards, a removed reaction and whatever else is not read in v1
    group = message.get("group_id")
    return InboundEvent(
        channel=Channel.whatsapp,
        external_message_id=message["id"],
        external_thread_id=group or handle,
        scope="group" if group else "dm",
        sender_handle=handle,
        sender_name=names.get(handle) or names.get(message.get("from", "")),
        text=text,
        media=media,
        reply_to_external_id=message.get("context", {}).get("id"),
        reaction_emoji=emoji,
        reaction_target_external_id=target if emoji else None,
        sent_at=datetime.fromtimestamp(int(message["timestamp"]), UTC),
        raw=payload,
    )


def _handle(message: dict[str, Any]) -> str | None:
    """Who wrote: the business-scoped user id, which is always sent, or failing that the phone
    number in E.164. The number is left out once someone has a username and has been quiet a month."""
    for value in (message.get("from_user_id"), message.get("from")):
        if value and _USER_ID.fullmatch(value):
            return str(value)
    phone = _digits(message.get("from"))
    return f"+{phone}" if phone else None


def _recipient(external_thread_id: str) -> dict[str, str]:
    """Address a send: a phone number, a business-scoped user id, or else a group id."""
    base = {"messaging_product": "whatsapp", "recipient_type": "individual"}
    if _PHONE.fullmatch(external_thread_id):
        return {**base, "to": external_thread_id[1:]}
    if _USER_ID.fullmatch(external_thread_id):
        return {**base, "recipient": external_thread_id}
    return {**base, "recipient_type": "group", "to": external_thread_id}


def _digits(value: str | None) -> str:
    return re.sub(r"\D", "", value or "")


def _mime(value: str | None) -> str | None:
    return value.split(";")[0].strip() if value else None   # "audio/ogg; codecs=opus"


def _error(errors: list[dict[str, Any]] | None) -> str | None:
    if not errors:
        return None
    first = errors[0]
    return f"{first.get('code', '')} {first.get('title') or first.get('message') or ''}".strip() or "failed"
