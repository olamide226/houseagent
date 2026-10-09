"""WhatsApp end to end: webhooks through the api, turns, and sends through the real adapters
(Graph and Bot API calls mocked with respx)."""
import json
from datetime import timedelta

import respx

from app.agent.loop import LoopRuntime
from app.channels.base import ADAPTERS
from app.core.envelope import OutboundMessage
from app.core.timeutil import utcnow
from app.db import execute, fetch_all, fetch_one, fetch_val, tx
from app.pipeline import inbound, router
from app.services import members
from tests.helpers import (
    FakeLLM,
    add_item,
    add_member,
    call,
    events_of,
    link,
    post_whatsapp,
    say,
    seed_home,
    wa_message,
    wa_webhook,
)
from tests.unit.test_dashboard import login

GRAPH = "https://graph.facebook.com/v26.0"
SEND = f"{GRAPH}/100000000000001/messages"
TG_SEND = "https://api.telegram.org/bot424242:TEST-TOKEN/sendMessage"
OLA, ADA = "GB.1000000000000000000101", "GB.1000000000000000000102"
GROUP = "Y2FwaV9ncm91cDo0NDc3MDA5MDAxMDA6MTIwMzYzMDAwMDAwMDAwMDAwZAZD"


def graph_send(n: int = 1):
    return respx.post(SEND).respond(json={"messaging_product": "whatsapp", "messages": [{"id": f"wamid.sent{n:04d}"}]})


def bodies(route) -> list[dict]:
    return [json.loads(c.request.content) for c in route.calls]


async def rows(sql, **params):
    async with tx() as conn:
        return await fetch_all(conn, sql, **params)


async def on_whatsapp(conn, *, telegram_id=None, **kwargs):
    home = await seed_home(conn, telegram_id=telegram_id)
    await link(conn, home.ola, OLA, "whatsapp", **kwargs)
    return home


async def process(home, *script):
    return await inbound.process_household(home.id, LoopRuntime(FakeLLM(*script)), ADAPTERS,
                                           public_base_url="http://testserver")


async def remind(home, text="Bins tonight", member=None):
    async with tx() as conn:
        await router.enqueue(conn, OutboundMessage(household_id=home.id, target="member", member_id=member or home.ola,
                                                   text=text, respect_quiet_hours=False))
    await router.dispatch_due(ADAPTERS)


# ---------------------------------------------------------------- webhook
async def test_metas_subscription_check_gets_the_challenge_back_only_with_our_verify_token(client):
    query = {"hub.mode": "subscribe", "hub.verify_token": "test-verify-token", "hub.challenge": "1158201444"}
    answer = await client.get("/webhooks/whatsapp", params=query)
    assert (answer.status_code, answer.text) == (200, "1158201444")
    assert answer.headers["content-type"].startswith("text/plain")
    assert (await client.get("/webhooks/whatsapp", params={**query, "hub.verify_token": "guess"})).status_code == 403
    assert (await client.get("/webhooks/whatsapp")).status_code == 403


async def test_a_webhook_whose_body_was_tampered_with_is_refused_and_stores_nothing(client):
    async with tx() as conn:
        await on_whatsapp(conn)
    payload = wa_message(1, "we're out of eggs")
    genuine = await post_whatsapp(client, payload)
    signature = genuine.request.headers["x-hub-signature-256"]
    assert genuine.status_code == 200 and len(await rows("select 1 from messages")) == 1

    forged = json.dumps(wa_message(2, "we're out of gold")).encode()
    for headers in ({"X-Hub-Signature-256": signature}, {"X-Hub-Signature-256": "sha256=" + "0" * 64}, {}):
        assert (await client.post("/webhooks/whatsapp", content=forged, headers=headers)).status_code == 401
    assert len(await rows("select 1 from messages")) == 1


async def test_a_members_message_is_stored_under_their_user_id_with_or_without_a_phone_number(client):
    async with tx() as conn:
        home = await on_whatsapp(conn)
    await post_whatsapp(client, wa_message(1, "we're out of eggs"))
    await post_whatsapp(client, wa_message(2, "and bread", phone=None))          # a username, no number shown
    stored = await rows("select m.text, m.member_id, m.status, t.channel, t.external_thread_id, t.scope "
                        "from messages m join threads t on t.id = m.thread_id order by m.created_at")
    assert stored == [
        {"text": text, "member_id": home.ola, "status": "received", "channel": "whatsapp",
         "external_thread_id": OLA, "scope": "dm"} for text in ("we're out of eggs", "and bread")]


async def test_strangers_on_whatsapp_are_invisible_and_an_invite_code_connects_the_sender(client):
    async with tx() as conn:
        home = await seed_home(conn)                                             # Ola, on Telegram only so far
        code = await members.create_invite(conn, home.ola, utcnow())
    await post_whatsapp(client, wa_message(1, "hello?"))
    await post_whatsapp(client, wa_message(2, "ZZZZ-9999"))
    assert await rows("select 1 from messages") == [] and await rows("select 1 from outbox") == []
    assert len(await rows("select 1 from channel_identities")) == 1

    await post_whatsapp(client, wa_message(3, code.lower(), phone=None))
    identity = await fetch_identity(home.ola)
    assert identity == {"handle": OLA, "preferred_channel": "telegram"}           # the first channel stays preferred

    with respx.mock:
        sent = graph_send()
        await router.dispatch_due(ADAPTERS)
    # Connecting opened the 24-hour window, so the welcome is an ordinary message, not the template.
    assert bodies(sent) == [{"messaging_product": "whatsapp", "recipient_type": "individual", "recipient": OLA,
                             "type": "text", "text": {"body": f"Hi Ola, you're connected. {inbound.WELCOME}"}}]


async def fetch_identity(member_id):
    async with tx() as conn:
        return await fetch_one(
            conn, "select ci.handle, m.preferred_channel from channel_identities ci join members m "
                  "on m.id = ci.member_id where m.id = :m and ci.channel = 'whatsapp'", m=member_id)


# ---------------------------------------------------------------- turns
@respx.mock
async def test_a_whatsapp_turn_is_acked_with_a_tick_and_a_replayed_webhook_changes_nothing(client):
    sent = graph_send()
    async with tx() as conn:
        home = await on_whatsapp(conn)
        await add_item(conn, home, "egg", location="fridge", staple=True, qty=6)
    payload = wa_message(501, "we're out of eggs")

    await post_whatsapp(client, payload)
    await post_whatsapp(client, payload)                                         # Meta retries before processing
    assert await process(home, call("log_inventory", changes=[{"item": "eggs", "action": "finished"}]), say("ACK")) == 1
    await router.dispatch_due(ADAPTERS)
    await post_whatsapp(client, payload)                                         # and again after
    assert await process(home) == 0
    await router.dispatch_due(ADAPTERS)

    async with tx() as conn:
        assert await events_of(conn, home) == [("egg", "finished", None, "message")]
    assert len(await rows("select 1 from messages where direction = 'in'")) == 1
    assert len(await rows("select 1 from agent_actions")) == 1
    assert [r["status"] for r in await rows("select status from outbox")] == ["sent"]
    assert bodies(sent) == [{"messaging_product": "whatsapp", "recipient_type": "individual", "recipient": OLA,
                             "type": "reaction", "reaction": {"message_id": "wamid.test0501", "emoji": "✅"}}]


@respx.mock
async def test_a_message_in_a_whatsapp_group_makes_it_the_family_chat_and_is_answered_there(client):
    sent = graph_send()
    async with tx() as conn:
        home = await on_whatsapp(conn)
        await add_member(conn, home, "Ada", whatsapp_id=ADA)
    await post_whatsapp(client, wa_message(1, "what's on the list?", user_id=ADA, phone="447700900102",
                                           name="Ada", group_id=GROUP))
    llm = FakeLLM(say("Nothing yet."))
    await inbound.process_household(home.id, LoopRuntime(llm), ADAPTERS)
    await router.dispatch_due(ADAPTERS)

    thread = await fetch_group(home)
    assert thread == {"external_thread_id": GROUP, "scope": "group", "channel": "whatsapp", "is_primary": True}
    assert llm.requests[0][1][-1].content[0].text == "Ada: what's on the list?"
    assert bodies(sent) == [{"messaging_product": "whatsapp", "recipient_type": "group", "to": GROUP,
                             "type": "text", "text": {"body": "Nothing yet."}}]


async def fetch_group(home):
    async with tx() as conn:
        return await fetch_one(
            conn, "select t.external_thread_id, t.scope, t.channel, t.id = h.primary_thread_id as is_primary "
                  "from threads t join households h on h.id = t.household_id "
                  "where h.id = :h and t.scope = 'group'", h=home.id)


@respx.mock
async def test_a_voice_note_is_downloaded_from_the_graph_api_and_transcribed(client):
    class StandInTranscriber:
        async def transcribe(self, audio: bytes, mime: str) -> str:
            assert (audio, mime) == (b"OggS-audio", "audio/ogg")
            return "we're out of eggs and bread"

    respx.get(f"{GRAPH}/900000000000502").respond(json={
        "url": "https://lookaside.example.net/media/502", "mime_type": "audio/ogg; codecs=opus", "id": "900000000000502"})
    respx.get("https://lookaside.example.net/media/502").respond(content=b"OggS-audio")
    graph_send()
    async with tx() as conn:
        home = await on_whatsapp(conn)
    await post_whatsapp(client, wa_message(1, type="audio", audio={
        "id": "900000000000502", "mime_type": "audio/ogg; codecs=opus", "voice": True}))
    llm = FakeLLM(say("ACK"))
    await inbound.process_household(home.id, LoopRuntime(llm), ADAPTERS, stt=StandInTranscriber())
    assert llm.requests[0][1][-1].content[0].text == "[voice note] we're out of eggs and bread"


# ---------------------------------------------------------------- the window, statuses and fallback
@respx.mock
async def test_a_reminder_outside_24_hours_goes_out_as_the_approved_template(client):
    sent = graph_send()
    async with tx() as conn:
        home = await on_whatsapp(conn, verified_at=utcnow() - timedelta(days=9))
    await post_whatsapp(client, wa_message(1, "dashboard"))                      # heard from just now...
    await process(home)
    await router.dispatch_due(ADAPTERS)
    assert [body["type"] for body in bodies(sent)] == ["text"]                   # ...so the link is plain text

    async with tx() as conn:                                                     # two days pass in silence
        await execute(conn, "update messages set created_at = created_at - interval '2 days'")
    await remind(home, "GP for Ada tomorrow\nat 10:30, Hurley Clinic")
    assert bodies(sent)[1] == {
        "messaging_product": "whatsapp", "recipient_type": "individual", "recipient": OLA, "type": "template",
        "template": {"name": "household_reminder", "language": {"code": "en_GB"}, "components": [{
            "type": "body", "parameters": [{"type": "text", "text": "GP for Ada tomorrow at 10:30, Hurley Clinic"}]}]}}
    (row,) = await rows("select status, channel_used, external_id from outbox where text like 'GP%'")
    assert row == {"status": "sent", "channel_used": "whatsapp", "external_id": "wamid.sent0001"}


@respx.mock
async def test_a_failed_status_from_meta_fails_the_send_and_moves_it_to_telegram_once(client):
    graph_send(7)
    telegram = respx.post(TG_SEND).respond(json={"ok": True, "result": {"message_id": 9001}})
    async with tx() as conn:
        home = await on_whatsapp(conn)
        await link(conn, home.ola, "1001")                                       # WhatsApp stays preferred
    await remind(home)
    assert telegram.call_count == 0

    failed = wa_webhook("messages", statuses=[
        {"id": "wamid.sent0007", "status": "sent", "timestamp": "1791230880", "recipient_user_id": OLA},
        {"id": "wamid.sent0007", "status": "failed", "timestamp": "1791230881", "recipient_user_id": OLA,
         "errors": [{"code": 131026, "title": "Message undeliverable"}]},
        {"id": "wamid.not-ours", "status": "failed", "timestamp": "1791230881",
         "errors": [{"code": 131026, "title": "Message undeliverable"}]}])
    assert (await post_whatsapp(client, failed)).status_code == 200
    assert (await post_whatsapp(client, failed)).status_code == 200              # delivered twice by Meta
    await router.dispatch_due(ADAPTERS)

    assert await rows("select status, channel_used, last_error from outbox order by created_at") == [
        {"status": "failed", "channel_used": "whatsapp", "last_error": "131026 Message undeliverable"},
        {"status": "sent", "channel_used": "telegram", "last_error": None}]
    assert bodies(telegram) == [{"chat_id": "1001", "text": "Bins tonight", "parse_mode": "HTML",
                                 "link_preview_options": {"is_disabled": True}}]
    assert await rows("select 1 from messages where direction = 'in'") == []     # a status is never a message


@respx.mock
async def test_a_send_meta_refuses_outright_moves_to_telegram_without_retrying(client):
    refused = respx.post(SEND).respond(400, json={"error": {"message": "Recipient is not a valid WhatsApp user",
                                                            "type": "OAuthException", "code": 131030}})
    telegram = respx.post(TG_SEND).respond(json={"ok": True, "result": {"message_id": 9001}})
    async with tx() as conn:
        home = await on_whatsapp(conn)
        await link(conn, home.ola, "1001")
    await remind(home)
    await router.dispatch_due(ADAPTERS)
    assert (refused.call_count, telegram.call_count) == (1, 1)
    assert [r["status"] for r in await rows("select status from outbox order by created_at")] == ["failed", "sent"]


# ---------------------------------------------------------------- preferred channel, end to end
@respx.mock
async def test_moving_a_members_preferred_channel_on_the_family_page_moves_their_next_dm(client):
    whatsapp = graph_send()
    telegram = respx.post(TG_SEND).respond(json={"ok": True, "result": {"message_id": 9001}})
    async with tx() as conn:
        home = await seed_home(conn)                                             # Telegram first, so preferred
        await link(conn, home.ola, OLA, "whatsapp")
    csrf = await login(client, home)

    await remind(home, "one")
    assert (telegram.call_count, whatsapp.call_count) == (1, 0)

    page = await client.post(f"/dashboard/family/{home.ola}/channel", data={"channel": "whatsapp"}, headers=csrf)
    assert page.status_code == 200
    await remind(home, "two")
    assert (telegram.call_count, whatsapp.call_count) == (1, 1)
    assert bodies(whatsapp)[0]["recipient"] == OLA and bodies(whatsapp)[0]["text"] == {"body": "two"}

    await client.post(f"/dashboard/family/{home.ola}/channel", data={"channel": "telegram"}, headers=csrf)
    await remind(home, "three")
    assert [body["text"] for body in bodies(telegram)] == ["one", "three"] and whatsapp.call_count == 1
    async with tx() as conn:
        assert await fetch_val(conn, "select count(*) from channel_identities") == 2   # nothing else changed
