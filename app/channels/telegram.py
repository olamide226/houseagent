"""Telegram Bot API adapter (spec section 6.1)."""
import hmac
import html
import json
from datetime import UTC, datetime
from typing import Any

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

API = "https://api.telegram.org"


class TelegramAdapter:
    channel = Channel.telegram
    capabilities = Capabilities(
        groups=True, reactions=True, ack_emoji="\U0001F44D", voice_in=True, images_in=True,
        threaded_replies=True, proactive_window_hours=None, max_text_len=4096, formatting="telegram_html",
    )

    def __init__(self, bot_token: str, webhook_secret: str) -> None:
        self._token = bot_token
        self._secret = webhook_secret
        self._bot_id = bot_token.split(":", 1)[0]   # a bot token starts with the bot's own user id

    async def verify(self, request: Request, body: bytes) -> None:
        given = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(given.encode(), self._secret.encode()):
            raise HTTPException(status_code=401)

    async def parse(self, body: bytes) -> list[InboundEvent]:
        update = json.loads(body)
        if "message" in update:
            event = self._message(update, update["message"])
        elif "message_reaction" in update:
            event = self._reaction(update, update["message_reaction"])
        else:
            event = None   # edited_message and everything else is ignored in v1
        return [event] if event else []

    async def parse_updates(self, body: bytes) -> list[DeliveryStatus | GroupUpdate]:
        return []   # Telegram reports a failed send in the reply to the send itself

    def _message(self, update: dict[str, Any], message: dict[str, Any]) -> InboundEvent | None:
        sender, chat = message.get("from"), message["chat"]
        scope = _scope(chat)
        if sender is None or scope is None or str(sender["id"]) == self._bot_id:
            return None
        media: list[MediaRef] = []
        if "voice" in message:
            voice = message["voice"]
            media.append(MediaRef(kind="audio", mime=voice.get("mime_type", "audio/ogg"),
                                  external_id=voice["file_id"]))
        if message.get("photo"):
            media.append(MediaRef(kind="image", mime="image/jpeg", external_id=message["photo"][-1]["file_id"]))
        if "location" in message:
            media.append(MediaRef(kind="location", lat=message["location"]["latitude"],
                                  lng=message["location"]["longitude"]))
        text = message.get("text") or message.get("caption")
        if text and text.startswith("/start "):
            text = text.removeprefix("/start ").strip()   # invite deep link: "/start CODE" is the code
        reply = message.get("reply_to_message")
        return InboundEvent(
            channel=Channel.telegram,
            external_message_id=str(message["message_id"]),
            external_thread_id=str(chat["id"]),
            scope=scope,
            sender_handle=str(sender["id"]),
            sender_name=sender.get("first_name"),
            text=text,
            media=media,
            reply_to_external_id=str(reply["message_id"]) if reply else None,
            sent_at=datetime.fromtimestamp(message["date"], UTC),
            raw=update,
        )

    def _reaction(self, update: dict[str, Any], reaction: dict[str, Any]) -> InboundEvent | None:
        sender, scope = reaction.get("user"), _scope(reaction["chat"])
        emoji = next((r["emoji"] for r in reaction.get("new_reaction", []) if r.get("type") == "emoji"), None)
        if sender is None or scope is None or emoji is None or str(sender["id"]) == self._bot_id:
            return None
        return InboundEvent(
            channel=Channel.telegram,
            external_message_id=f"reaction:{update['update_id']}",   # reactions carry no id of their own
            external_thread_id=str(reaction["chat"]["id"]),
            scope=scope,
            sender_handle=str(sender["id"]),
            sender_name=sender.get("first_name"),
            reaction_emoji=emoji,
            reaction_target_external_id=str(reaction["message_id"]),
            sent_at=datetime.fromtimestamp(reaction["date"], UTC),
            raw=update,
        )

    async def fetch_media(self, ref: MediaRef) -> tuple[bytes, str]:
        file = await self._call("getFile", {"file_id": ref.external_id})
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.get(f"{API}/file/bot{self._token}/{file['file_path']}")
        if response.status_code != 200:
            raise ChannelError(f"telegram file download failed: HTTP {response.status_code}")
        return response.content, ref.mime or "application/octet-stream"

    async def send_text(self, external_thread_id: str, text: str,
                        reply_to_external_id: str | None = None) -> SendResult:
        payload: dict[str, Any] = {"chat_id": external_thread_id, "text": text, "parse_mode": "HTML"}
        if reply_to_external_id:
            payload["reply_parameters"] = {"message_id": int(reply_to_external_id)}
        sent = await self._call("sendMessage", payload)
        return SendResult(external_id=str(sent["message_id"]))

    async def react(self, external_thread_id: str, external_message_id: str, emoji: str) -> None:
        await self._call("setMessageReaction", {
            "chat_id": external_thread_id, "message_id": int(external_message_id),
            "reaction": [{"type": "emoji", "emoji": emoji}],
        })

    async def send_template(self, external_thread_id: str, name: str, params: list[str]) -> SendResult:
        raise NotSupported("telegram has no message templates")

    def dm_thread_id(self, handle: str) -> str:
        return handle   # a private chat id equals the user id

    def format(self, text: str) -> str:
        return html.escape(text, quote=False)   # sends use parse_mode=HTML

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(f"{API}/bot{self._token}/{method}", json=payload)
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Never include the exception text: httpx errors can carry the URL, which holds the token.
            raise ChannelError(f"telegram {method} failed: {type(exc).__name__}") from None
        if not data.get("ok"):
            # 4xx other than "slow down" is final: the chat is gone, the bot is blocked, the token is wrong.
            final = 400 <= response.status_code < 500 and response.status_code != 429
            raise (PermanentError if final else ChannelError)(
                f"telegram {method} failed: {data.get('description', response.status_code)}")
        return data["result"]


def _scope(chat: dict[str, Any]) -> Any:
    return {"private": "dm", "group": "group", "supergroup": "group"}.get(chat.get("type", ""))
