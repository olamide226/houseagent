"""WhatsApp adapter: signature and subscription checks, parse edge cases, statuses, group
updates and Graph API calls (mocked with respx)."""
import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import HTTPException
from starlette.requests import Request

from app.channels.base import ChannelError, GroupHost, PermanentError
from app.channels.whatsapp import WhatsAppAdapter
from app.core.envelope import DeliveryStatus, GroupUpdate, MediaRef

FIXTURES = Path(__file__).parent / "fixtures" / "whatsapp"
TOKEN, SECRET = "test-access-token", "test-app-secret"
GRAPH = "https://graph.facebook.com/v26.0"
SEND = f"{GRAPH}/100000000000001/messages"
OLA = "GB.1000000000000000000101"
GROUP = "Y2FwaV9ncm91cDo0NDc3MDA5MDAxMDA6MTIwMzYzMDAwMDAwMDAwMDAwZAZD"
SENT = {"messaging_product": "whatsapp", "contacts": [{"input": OLA, "user_id": OLA}],
        "messages": [{"id": "wamid.HBgMsent0001"}]}


@pytest.fixture
def adapter():
    return WhatsAppAdapter("100000000000001", TOKEN, SECRET, "test-verify-token",
                           reminder_template="household_reminder")


def signed(body: bytes, secret: str = SECRET, prefix: str = "sha256=") -> Request:
    signature = prefix + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return Request({"type": "http", "method": "POST", "path": "/",
                    "headers": [(b"x-hub-signature-256", signature.encode())]})


def body_of(route) -> dict:
    return json.loads(route.calls.last.request.content)


def webhook(*messages, contacts=(), field="messages", **value) -> bytes:
    value = {"messaging_product": "whatsapp",
             "metadata": {"display_phone_number": "+44 7700 900100", "phone_number_id": "100000000000001"},
             "contacts": list(contacts), **({"messages": list(messages)} if messages else {}), **value}
    return json.dumps({"object": "whatsapp_business_account",
                       "entry": [{"id": "200000000000002", "changes": [{"value": value, "field": field}]}]}).encode()


def message(**body) -> dict:
    return {"from": "447700900101", "from_user_id": OLA, "id": "wamid.HBgMexample0001", "timestamp": "1791230400",
            **body}


# ---------------------------------------------------------------- verification
async def test_verify_accepts_only_a_signature_of_this_exact_body_with_the_app_secret(adapter):
    body = (FIXTURES / "text.json").read_bytes()
    await adapter.verify(signed(body), body)

    tampered = body.replace(b"out of eggs", b"out of gold")
    refused = [(signed(body), tampered),                                 # body changed after signing
               (signed(body, secret="someone-elses-secret"), body),      # signed with another secret
               (signed(body, prefix="sha1="), body),                     # the old header format
               (signed(body, prefix=""), body),
               (Request({"type": "http", "method": "POST", "path": "/", "headers": []}), body)]
    for request, received in refused:
        with pytest.raises(HTTPException) as caught:
            await adapter.verify(request, received)
        assert caught.value.status_code == 401


def test_the_subscription_check_echoes_the_challenge_only_for_our_verify_token(adapter):
    assert adapter.subscription_challenge("subscribe", "test-verify-token", "1158201444") == "1158201444"
    for mode, token in [("subscribe", "wrong"), ("subscribe", ""), ("unsubscribe", "test-verify-token")]:
        with pytest.raises(HTTPException) as caught:
            adapter.subscription_challenge(mode, token, "1158201444")
        assert caught.value.status_code == 403


# ---------------------------------------------------------------- parsing
async def test_the_handle_is_the_user_id_and_falls_back_to_the_phone_number_in_e164(adapter):
    (with_both,) = await adapter.parse(webhook(message(type="text", text={"body": "hi"})))
    assert (with_both.sender_handle, with_both.external_thread_id) == (OLA, OLA)

    old_shape = message(type="text", text={"body": "hi"})
    del old_shape["from_user_id"]
    (phone_only,) = await adapter.parse(webhook(old_shape, contacts=[{"profile": {"name": "Ola"},
                                                                    "wa_id": "447700900101"}]))
    assert (phone_only.sender_handle, phone_only.sender_name) == ("+447700900101", "Ola")

    old_shape["from"] = OLA                       # a user id where the number used to be is still a user id
    (moved,) = await adapter.parse(webhook(old_shape))
    assert moved.sender_handle == OLA


async def test_a_message_from_the_business_number_itself_is_dropped(adapter):
    echoed = message(type="text", text={"body": "Added milk."})
    echoed["from"] = "447700900100"
    assert await adapter.parse(webhook(echoed)) == []


async def test_button_replies_and_document_captions_become_text(adapter):
    (button,) = await adapter.parse(webhook(message(
        type="interactive", interactive={"type": "button_reply", "button_reply": {"id": "yes", "title": "Yes"}})))
    assert button.text == "Yes"
    (letter,) = await adapter.parse(webhook(message(
        type="document", document={"id": "900000000000901", "mime_type": "application/pdf",
                                   "filename": "letter.pdf", "caption": "GP letter"})))
    assert letter.text == "GP letter"
    assert letter.media == [MediaRef(kind="document", mime="application/pdf", external_id="900000000000901")]


async def test_stickers_removed_reactions_and_other_fields_are_ignored(adapter):
    sticker = message(type="sticker", sticker={"id": "900000000000902", "mime_type": "image/webp"})
    removed = message(type="reaction", reaction={"message_id": "wamid.HBgMexample0777", "emoji": ""})
    unsupported = message(type="unsupported", errors=[{"code": 131051, "title": "Unsupported message type"}])
    assert await adapter.parse(webhook(sticker, removed, unsupported)) == []
    account_update = webhook(message(type="text", text={"body": "not a message change"}), field="account_update")
    assert await adapter.parse(account_update) == []


async def test_one_webhook_can_carry_several_messages(adapter):
    second = message(type="text", text={"body": "and bread"})
    second["id"] = "wamid.HBgMexample0002"
    events = await adapter.parse(webhook(message(type="text", text={"body": "out of eggs"}), second))
    assert [(e.external_message_id, e.text) for e in events] == [
        ("wamid.HBgMexample0001", "out of eggs"), ("wamid.HBgMexample0002", "and bread")]


async def test_statuses_and_group_outcomes_are_reported_as_updates_not_messages(adapter):
    assert await adapter.parse_updates((FIXTURES / "status.json").read_bytes()) == [
        DeliveryStatus(channel="whatsapp", external_message_id="wamid.HBgMexample0777", status="delivered"),
        DeliveryStatus(channel="whatsapp", external_message_id="wamid.HBgMexample0780", status="failed",
                       error="131047 Re-engagement message"),
    ]
    assert await adapter.parse_updates((FIXTURES / "group_created.json").read_bytes()) == [
        GroupUpdate(channel="whatsapp", subject="Adebayo family", external_thread_id=GROUP)]
    failed = webhook(field="group_lifecycle_update", groups=[{
        "timestamp": "1791230900", "type": "group_create", "subject": "Adebayo family", "request_id": "r1",
        "group_id": GROUP, "errors": [{"code": 131215, "title": "Groups not eligible", "message": "Not eligible"}]}])
    assert await adapter.parse_updates(failed) == [
        GroupUpdate(channel="whatsapp", subject="Adebayo family", error="131215 Groups not eligible")]
    assert await adapter.parse_updates((FIXTURES / "text.json").read_bytes()) == []


# ---------------------------------------------------------------- Graph API calls
@respx.mock
async def test_send_text_addresses_a_user_id_a_phone_number_and_a_group_each_in_its_own_way(adapter):
    route = respx.post(SEND).respond(json=SENT)
    sent = await adapter.send_text(OLA, "Milk is on the list.", reply_to_external_id="wamid.HBgMexample0501")
    assert sent.external_id == "wamid.HBgMsent0001"
    assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"
    assert body_of(route) == {
        "messaging_product": "whatsapp", "recipient_type": "individual", "recipient": OLA,
        "type": "text", "text": {"body": "Milk is on the list."}, "context": {"message_id": "wamid.HBgMexample0501"}}

    await adapter.send_text("+447700900101", "hello")
    assert body_of(route) == {"messaging_product": "whatsapp", "recipient_type": "individual",
                              "to": "447700900101", "type": "text", "text": {"body": "hello"}}

    await adapter.send_text(GROUP, "Bins tonight.")
    assert body_of(route) == {"messaging_product": "whatsapp", "recipient_type": "group", "to": GROUP,
                              "type": "text", "text": {"body": "Bins tonight."}}


@respx.mock
async def test_react_sends_a_reaction_to_the_message(adapter):
    route = respx.post(SEND).respond(json=SENT)
    await adapter.react(OLA, "wamid.HBgMexample0501", adapter.capabilities.ack_emoji)
    assert body_of(route) == {
        "messaging_product": "whatsapp", "recipient_type": "individual", "recipient": OLA,
        "type": "reaction", "reaction": {"message_id": "wamid.HBgMexample0501", "emoji": "✅"}}


@respx.mock
async def test_send_template_fills_the_one_body_parameter_without_line_breaks(adapter):
    route = respx.post(SEND).respond(json=SENT)
    sent = await adapter.send_template(OLA, "household_reminder", ["Today:\n- 09:00 Chatterbox\t(Tobi)\n\n- bins"])
    assert sent.external_id == "wamid.HBgMsent0001"
    assert body_of(route) == {
        "messaging_product": "whatsapp", "recipient_type": "individual", "recipient": OLA, "type": "template",
        "template": {"name": "household_reminder", "language": {"code": "en_GB"}, "components": [
            {"type": "body", "parameters": [{"type": "text", "text": "Today: - 09:00 Chatterbox (Tobi) - bins"}]}]}}


@respx.mock
async def test_fetch_media_looks_up_the_short_lived_url_then_downloads_with_the_token(adapter):
    lookup = respx.get(f"{GRAPH}/900000000000502").respond(json={
        "messaging_product": "whatsapp", "url": "https://lookaside.example.net/whatsapp_business/attachments/?mid=502",
        "mime_type": "audio/ogg; codecs=opus", "sha256": "x", "file_size": 18423, "id": "900000000000502"})
    download = respx.get("https://lookaside.example.net/whatsapp_business/attachments/?mid=502").respond(
        content=b"OggS-audio")
    data, mime = await adapter.fetch_media(MediaRef(kind="audio", mime="audio/ogg", external_id="900000000000502"))
    assert (data, mime) == (b"OggS-audio", "audio/ogg")
    assert lookup.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"
    assert download.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"

    respx.get("https://lookaside.example.net/whatsapp_business/attachments/?mid=502").respond(404)
    with pytest.raises(ChannelError, match="HTTP 404"):
        await adapter.fetch_media(MediaRef(kind="audio", external_id="900000000000502"))


@respx.mock
async def test_a_refusal_is_final_but_throttling_server_and_network_errors_are_retried(adapter):
    def graph_error(status, code, text):
        return httpx.Response(status, json={"error": {"message": text, "type": "OAuthException", "code": code}})

    respx.post(SEND).mock(return_value=graph_error(400, 131026, "Message undeliverable"))
    with pytest.raises(PermanentError, match="131026 Message undeliverable"):
        await adapter.send_text(OLA, "hi")
    respx.post(SEND).mock(return_value=graph_error(401, 190, "Session has expired"))
    with pytest.raises(PermanentError):
        await adapter.send_text(OLA, "hi")

    for response in (graph_error(400, 131056, "Pair rate limit hit"), graph_error(429, 80007, "Rate limit hit"),
                     graph_error(500, 131000, "Something went wrong"), httpx.Response(502, text="Bad gateway")):
        respx.post(SEND).mock(return_value=response)
        with pytest.raises(ChannelError) as caught:
            await adapter.send_text(OLA, "hi")
        assert not isinstance(caught.value, PermanentError)

    respx.post(SEND).mock(side_effect=httpx.ConnectError(f"boom {TOKEN}"))
    with pytest.raises(ChannelError) as caught:
        await adapter.send_text(OLA, "hi")
    assert TOKEN not in str(caught.value) and caught.value.__cause__ is None
    assert not isinstance(caught.value, PermanentError)


@respx.mock
async def test_a_group_is_asked_for_by_name_and_its_invite_link_is_fetched_by_id(adapter):
    assert isinstance(adapter, GroupHost)
    create = respx.post(f"{GRAPH}/100000000000001/groups").respond(json={"messaging_product": "whatsapp"})
    assert await adapter.create_group("Adebayo family") is None        # the id comes later, by webhook
    assert body_of(create) == {"messaging_product": "whatsapp", "subject": "Adebayo family"}
    respx.post(f"{GRAPH}/100000000000001/groups").respond(json={"messaging_product": "whatsapp", "id": GROUP})
    assert await adapter.create_group("Adebayo family") == GROUP

    respx.get(f"{GRAPH}/{GROUP}/invite_link").respond(json={
        "messaging_product": "whatsapp", "invite_link": "https://chat.whatsapp.com/EXAMPLEinviteLINK01"})
    assert await adapter.invite_link(GROUP) == "https://chat.whatsapp.com/EXAMPLEinviteLINK01"


def test_the_channel_has_a_24_hour_window_a_template_and_a_tick_for_an_ack(adapter):
    caps = adapter.capabilities
    assert (caps.proactive_window_hours, caps.proactive_template, caps.ack_emoji) == (24, "household_reminder", "✅")
    assert adapter.dm_thread_id(OLA) == OLA and adapter.format("2 < 3 & *bold*") == "2 < 3 & *bold*"
