"""iMessage adapter: the webhook secret, parse edge cases, send failures reported later, and
BlueBubbles API calls (mocked with respx)."""
import json
import uuid
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import HTTPException
from starlette.requests import Request

from app.channels.base import ChannelError, HealthChecked, NotSupported, PermanentError
from app.channels.imessage import IMessageAdapter
from app.core.envelope import Channel, DeliveryStatus, MediaRef

FIXTURES = Path(__file__).parent / "fixtures" / "imessage"
BB = "http://mac-mini.test:1234/api/v1"
PASSWORD, SECRET = "test-bb-password", "test-bb-secret"
OLA_DM = "iMessage;-;+447700900101"
GROUP = "iMessage;+;chat610000000000000042"
SENT = {"status": 200, "message": "Message sent!", "data": {"guid": "5C0FFEE0-9001-4A6B-9C1D-000000009001"}}


@pytest.fixture
def adapter():
    return IMessageAdapter("http://mac-mini.test:1234/", PASSWORD, SECRET)


@pytest.fixture
def private():
    return IMessageAdapter("http://mac-mini.test:1234", PASSWORD, SECRET, private_api=True)


def request_with(query: str) -> Request:
    return Request({"type": "http", "method": "POST", "path": "/webhooks/imessage", "headers": [],
                    "query_string": query.encode()})


def recorded(case: str, **changes) -> bytes:
    payload = json.loads((FIXTURES / f"{case}.json").read_text())
    payload["data"].update(changes)
    return json.dumps(payload).encode()


def body_of(route) -> dict:
    return json.loads(route.calls.last.request.content)


# ---------------------------------------------------------------- verification
async def test_verify_accepts_only_the_secret_in_the_webhook_url(adapter):
    await adapter.verify(request_with(f"secret={SECRET}"), b"{}")
    for wrong in ("secret=nope", "secret=", "", f"password={SECRET}", f"secret={SECRET}x"):
        with pytest.raises(HTTPException) as caught:
            await adapter.verify(request_with(wrong), b"{}")
        assert caught.value.status_code == 401


# ---------------------------------------------------------------- parsing
async def test_only_new_messages_from_other_people_become_events(adapter):
    assert len(await adapter.parse(recorded("text"))) == 1
    ignored = [
        json.dumps({"type": "updated-message", "data": json.loads(recorded("text"))["data"]}).encode(),
        json.dumps({"type": "typing-indicator", "data": {"display": True, "guid": OLA_DM}}).encode(),
        json.dumps({"type": "new-message"}).encode(),
        recorded("text", isFromMe=True),
        recorded("text", handle=None),          # the server could not say who wrote
        recorded("text", text=None),            # nothing to read
        recorded("reaction", associatedMessageType="sticker"),
        recorded("reaction", associatedMessageType="2006"),      # a kind the server has no name for
    ]
    for body in ignored:
        assert await adapter.parse(body) == []


async def test_a_dm_is_the_chat_with_its_sender_whatever_the_server_calls_it(adapter):
    """A reply has to land where `dm_thread_id` says that person's DM is, or the router would
    not recognise the thread as theirs."""
    for chats in ([{"guid": "SMS;-;+447700900101", "style": 45}], [{"guid": "any;-;+447700900101", "style": 45}], []):
        (event,) = await adapter.parse(recorded("text", chats=chats))
        assert (event.scope, event.external_thread_id) == ("dm", OLA_DM)
        assert event.external_thread_id == adapter.dm_thread_id(event.sender_handle)
    (event,) = await adapter.parse(recorded("group", chats=[{"guid": GROUP}]))   # the guid alone says group
    assert (event.scope, event.external_thread_id) == ("group", GROUP)


async def test_each_tapback_kind_maps_to_its_emoji_on_the_message_it_is_about(adapter):
    seen = {}
    for kind in ("love", "like", "dislike", "laugh", "emphasize", "question"):
        (event,) = await adapter.parse(recorded("reaction", associatedMessageType=kind))
        assert event.text is None and event.reaction_target_external_id == "5C0FFEE0-0777-4A6B-9C1D-000000000777"
        seen[kind] = event.reaction_emoji
    assert seen == {"love": "❤️", "like": "\U0001F44D", "dislike": "\U0001F44E", "laugh": "\U0001F602",
                    "emphasize": "‼️", "question": "❓"}
    (bubble,) = await adapter.parse(recorded("reaction", associatedMessageGuid="bp:5C0FFEE0-0777"))
    assert bubble.reaction_target_external_id == "5C0FFEE0-0777"


async def test_a_send_the_mac_reports_as_failed_is_a_failed_delivery_status(adapter):
    assert await adapter.parse_updates((FIXTURES / "send_error.json").read_bytes()) == [DeliveryStatus(
        channel=Channel.imessage, external_message_id="5C0FFEE0-0901-4A6B-9C1D-000000000901", status="failed",
        error="iMessage error 22")]
    assert await adapter.parse_updates((FIXTURES / "text.json").read_bytes()) == []
    assert await adapter.parse_updates(b'{"type": "message-send-error", "data": null}') == []


# ---------------------------------------------------------------- sending
@respx.mock
async def test_send_text_posts_to_the_chat_with_the_password_and_returns_the_message_guid(adapter):
    route = respx.post(f"{BB}/message/text").respond(json=SENT)
    sent = await adapter.send_text(OLA_DM, "Added eggs & bread.", reply_to_external_id="5C0FFEE0-0501")
    assert sent.external_id == "5C0FFEE0-9001-4A6B-9C1D-000000009001"
    request = route.calls.last.request
    assert dict(request.url.params) == {"password": PASSWORD}
    body = body_of(route)
    assert uuid.UUID(body.pop("tempGuid")).version == 4          # AppleScript sends are refused without one
    # Without the Private API there are no threaded replies: the text goes as an ordinary message.
    assert body == {"chatGuid": OLA_DM, "message": "Added eggs & bread.", "method": "apple-script"}
    await adapter.send_text(OLA_DM, "again")
    assert json.loads(route.calls[0].request.content)["tempGuid"] != body_of(route)["tempGuid"]


@respx.mock
async def test_with_the_private_api_a_reply_is_threaded_and_an_ack_is_a_tapback(private):
    text = respx.post(f"{BB}/message/text").respond(json=SENT)
    react = respx.post(f"{BB}/message/react").respond(json={"status": 200, "message": "Reaction sent!", "data": {}})
    await private.send_text(GROUP, "On the list.", reply_to_external_id="5C0FFEE0-0778")
    assert {k: v for k, v in body_of(text).items() if k != "tempGuid"} == {
        "chatGuid": GROUP, "message": "On the list.", "method": "private-api", "selectedMessageGuid": "5C0FFEE0-0778"}
    await private.send_text(GROUP, "No reply")
    assert "selectedMessageGuid" not in body_of(text)

    for emoji, kind in (("✅", "like"), ("\U0001F44D", "like"), ("❤️", "love"), ("❓", "question")):
        await private.react(GROUP, "5C0FFEE0-0778", emoji)
        assert body_of(react) == {"chatGuid": GROUP, "selectedMessageGuid": "5C0FFEE0-0778", "reaction": kind,
                                  "partIndex": 0}
        assert dict(react.calls.last.request.url.params) == {"password": PASSWORD}
    with pytest.raises(NotSupported):
        await private.react(GROUP, "5C0FFEE0-0778", "\U0001F389")   # iMessage has six tapbacks and no party popper
    assert react.call_count == 4


@respx.mock
async def test_without_the_private_api_tapbacks_and_templates_are_not_supported(adapter):
    with pytest.raises(NotSupported):
        await adapter.react(OLA_DM, "5C0FFEE0-0501", "✅")
    with pytest.raises(NotSupported):
        await adapter.send_template(OLA_DM, "household_reminder", ["Bins tonight"])
    assert not respx.calls


def test_capabilities_follow_the_private_api_setting(adapter, private):
    assert (adapter.capabilities.reactions, adapter.capabilities.threaded_replies) == (False, False)
    assert (private.capabilities.reactions, private.capabilities.threaded_replies) == (True, True)
    for one in (adapter, private):
        caps = one.capabilities
        assert (caps.ack_emoji, caps.max_text_len, caps.proactive_window_hours, caps.proactive_template,
                caps.formatting, caps.groups) == ("✅", 10000, None, None, "plain", True)
        assert one.format("2 < 3 & *bold*") == "2 < 3 & *bold*"
        assert one.dm_thread_id("ada@example.com") == "iMessage;-;ada@example.com"
        assert isinstance(one, HealthChecked) and one.degraded is False


@respx.mock
async def test_a_refusal_is_final_and_a_mac_in_trouble_is_retried_and_neither_leaks_the_password(adapter):
    route = respx.post(f"{BB}/message/text")
    refused = {"status": 400, "message": "You've made a bad request!",
               "error": {"type": "Validation Error", "message": "Chat does not exist!"}}
    for status, reply in ((400, refused), (401, {"status": 401, "error": {"message": "Unauthorized"}}), (404, {})):
        route.respond(status, json=reply)
        with pytest.raises(PermanentError) as final:
            await adapter.send_text(OLA_DM, "hello")
        assert f"HTTP {status}" in str(final.value) and PASSWORD not in str(final.value)
    assert "Chat does not exist!" in str(await _raised(adapter, route, 400, refused))

    trouble = {"status": 500, "message": "Message Send Error", "error": {"type": "iMessage Error", "message": "x"}}
    for response in (httpx.Response(500, json=trouble), httpx.Response(502, text="Bad Gateway")):
        route.mock(return_value=response)
        with pytest.raises(ChannelError) as retried:
            await adapter.send_text(OLA_DM, "hello")
        assert not isinstance(retried.value, PermanentError)
    for error in (httpx.ConnectError(f"cannot reach {BB}/message/text?password={PASSWORD}"), httpx.ReadTimeout("slow")):
        route.mock(side_effect=error)
        with pytest.raises(ChannelError) as unreachable:
            await adapter.send_text(OLA_DM, "hello")
        assert not isinstance(unreachable.value, PermanentError)
        assert PASSWORD not in str(unreachable.value) and unreachable.value.__cause__ is None
    route.mock(return_value=httpx.Response(200, text="<html>tailscale login</html>"))
    with pytest.raises(ChannelError):
        await adapter.send_text(OLA_DM, "hello")


async def _raised(adapter, route, status, reply) -> Exception:
    route.respond(status, json=reply)
    with pytest.raises(ChannelError) as caught:
        await adapter.send_text(OLA_DM, "hello")
    return caught.value


# ---------------------------------------------------------------- media
@respx.mock
async def test_fetch_media_downloads_the_attachment_and_names_its_type(adapter):
    guid = "at_0_5C0FFEE0-0503-4A6B-9C1D-000000000503"
    photo = respx.get(f"{BB}/attachment/{guid}/download").respond(content=b"\xff\xd8jpeg", content_type="image/jpeg")
    assert await adapter.fetch_media(MediaRef(kind="image", mime="image/heic", external_id=guid)) == (
        b"\xff\xd8jpeg", "image/jpeg")                        # the server converts HEIC; its answer names the type
    assert dict(photo.calls.last.request.url.params) == {"password": PASSWORD}

    respx.get(f"{BB}/attachment/voice-1/download").respond(content=b"ID3mp3", content_type="audio/mp3")
    assert await adapter.fetch_media(MediaRef(kind="audio", mime="audio/x-caf", external_id="voice-1")) == (
        b"ID3mp3", "audio/mpeg")                              # a voice note the server converted itself
    respx.get(f"{BB}/attachment/voice-2/download").respond(content=b"caff\x00\x01desc", content_type="audio/mp3")
    assert (await adapter.fetch_media(MediaRef(kind="audio", external_id="voice-2")))[1] == "audio/x-caf"
    respx.get(f"{BB}/attachment/doc-1/download").respond(content=b"%PDF")
    assert (await adapter.fetch_media(MediaRef(kind="document", mime="application/pdf", external_id="doc-1")))[1] == (
        "application/pdf")                                    # no type in the answer: what the webhook said

    odd = respx.get(url__startswith=f"{BB}/attachment/a%2Fb%3Fx/download").respond(content=b"x")
    await adapter.fetch_media(MediaRef(kind="document", external_id="a/b?x"))
    assert odd.called                                         # an id cannot change the path or add a parameter
    respx.get(f"{BB}/attachment/gone/download").respond(404, json={"error": {"message": "Attachment does not exist!"}})
    with pytest.raises(PermanentError):
        await adapter.fetch_media(MediaRef(kind="image", external_id="gone"))


# ---------------------------------------------------------------- health
@respx.mock
async def test_ping_is_true_only_when_the_server_answers_pong(adapter):
    route = respx.get(f"{BB}/ping").respond(json={"status": 200, "message": "Ping received!", "data": "pong"})
    assert await adapter.ping() is True
    assert dict(route.calls.last.request.url.params) == {"password": PASSWORD}
    for response in (httpx.Response(500, json={}), httpx.Response(401, json={}), httpx.Response(200, json={"data": "x"}),
                     httpx.Response(200, text="not json"), httpx.Response(200, json=["pong"])):
        route.mock(return_value=response)
        assert await adapter.ping() is False
    for error in (httpx.ConnectError("refused"), httpx.ConnectTimeout("asleep")):
        route.mock(side_effect=error)
        assert await adapter.ping() is False
