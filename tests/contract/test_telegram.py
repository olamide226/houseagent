"""Telegram adapter: webhook verification, parse edge cases and Bot API calls (mocked with respx)."""
import json

import httpx
import pytest
import respx
from fastapi import HTTPException
from starlette.requests import Request

from app.channels.base import ChannelError, NotSupported
from app.channels.telegram import TelegramAdapter
from app.core.envelope import MediaRef

TOKEN = "424242:TEST-TOKEN"
API = f"https://api.telegram.org/bot{TOKEN}"


@pytest.fixture
def adapter():
    return TelegramAdapter(TOKEN, "test-webhook-secret")


def request_with(secret: str | None) -> Request:
    headers = [(b"x-telegram-bot-api-secret-token", secret.encode())] if secret is not None else []
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


async def test_verify_accepts_only_the_configured_secret(adapter):
    await adapter.verify(request_with("test-webhook-secret"), b"{}")
    for wrong in ("nope", "", None):
        with pytest.raises(HTTPException) as caught:
            await adapter.verify(request_with(wrong), b"{}")
        assert caught.value.status_code == 401


def update(**message):
    base = {"message_id": 9, "date": 1791230400, "from": {"id": 1001, "is_bot": False, "first_name": "Ola"},
            "chat": {"id": 1001, "type": "private"}}
    return json.dumps({"update_id": 1, "message": {**base, **message}}).encode()


async def test_start_deep_link_becomes_the_invite_code(adapter):
    (event,) = await adapter.parse(update(text="/start ABCD-2345"))
    assert event.text == "ABCD-2345"


async def test_edited_messages_channel_posts_and_removed_reactions_are_ignored(adapter):
    edited = json.dumps({"update_id": 2, "edited_message": json.loads(update(text="x"))["message"]}).encode()
    channel_post = update(chat={"id": -100, "type": "channel"}, text="news")
    removed = json.dumps({"update_id": 3, "message_reaction": {
        "chat": {"id": 1001, "type": "private"}, "message_id": 9, "date": 1791230400,
        "user": {"id": 1001, "is_bot": False, "first_name": "Ola"},
        "old_reaction": [{"type": "emoji", "emoji": "\U0001F44D"}], "new_reaction": []}}).encode()
    for body in (edited, channel_post, removed):
        assert await adapter.parse(body) == []


@respx.mock
async def test_send_text_posts_html_and_returns_the_message_id(adapter):
    route = respx.post(f"{API}/sendMessage").respond(json={"ok": True, "result": {"message_id": 9001}})
    sent = await adapter.send_text("1001", adapter.format("eggs < 6 & milk"), reply_to_external_id="501")
    assert sent.external_id == "9001"
    assert json.loads(route.calls.last.request.content) == {
        "chat_id": "1001", "text": "eggs &lt; 6 &amp; milk", "parse_mode": "HTML",
        "reply_parameters": {"message_id": 501},
    }


@respx.mock
async def test_react_sets_one_emoji_reaction(adapter):
    route = respx.post(f"{API}/setMessageReaction").respond(json={"ok": True, "result": True})
    await adapter.react("-1001234567890", "777", adapter.capabilities.ack_emoji)
    assert json.loads(route.calls.last.request.content) == {
        "chat_id": "-1001234567890", "message_id": 777, "reaction": [{"type": "emoji", "emoji": "\U0001F44D"}],
    }


@respx.mock
async def test_fetch_media_resolves_the_file_path_then_downloads(adapter):
    respx.post(f"{API}/getFile").respond(json={"ok": True, "result": {"file_id": "F1", "file_path": "voice/file_7.oga"}})
    respx.get(f"https://api.telegram.org/file/bot{TOKEN}/voice/file_7.oga").respond(content=b"OggS-audio")
    data, mime = await adapter.fetch_media(MediaRef(kind="audio", mime="audio/ogg", external_id="F1"))
    assert (data, mime) == (b"OggS-audio", "audio/ogg")


@respx.mock
async def test_api_and_network_errors_raise_without_leaking_the_token(adapter):
    respx.post(f"{API}/sendMessage").respond(400, json={"ok": False, "description": "Bad Request: chat not found"})
    with pytest.raises(ChannelError, match="chat not found"):
        await adapter.send_text("1", "hi")
    respx.post(f"{API}/sendMessage").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ChannelError) as caught:
        await adapter.send_text("1", "hi")
    assert TOKEN not in str(caught.value) and caught.value.__cause__ is None


async def test_templates_are_not_supported_and_dm_thread_is_the_user_id(adapter):
    with pytest.raises(NotSupported):
        await adapter.send_template("1001", "household_reminder", ["x"])
    assert adapter.dm_thread_id("1001") == "1001"
