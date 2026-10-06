"""iMessage end to end: BlueBubbles webhooks through the api, turns, sends through the real
adapters (BlueBubbles and Bot API calls mocked with respx), and what happens while BlueBubbles
does not answer."""
import json
import shutil
import subprocess
import wave
from datetime import timedelta
from io import BytesIO

import httpx
import pytest
import respx

from app.agent.loop import LoopRuntime
from app.channels.base import ADAPTERS
from app.channels.imessage import IMessageAdapter
from app.core.envelope import Channel, OutboundMessage
from app.core.timeutil import utcnow
from app.db import execute, fetch_all, fetch_one, tx
from app.pipeline import inbound, router
from app.pipeline import media as media_pipeline
from app.services import members
from app.worker import jobs
from tests.helpers import (
    FakeLLM,
    add_item,
    add_member,
    bb_message,
    call,
    events_of,
    link,
    london,
    post_imessage,
    say,
    seed_home,
)

BB = "http://mac-mini.test:1234/api/v1"
BB_SEND, BB_PING = f"{BB}/message/text", f"{BB}/ping"
TG_SEND = "https://api.telegram.org/bot424242:TEST-TOKEN/sendMessage"
OLA, ADA = "+447700900101", "ada@example.com"
OLA_DM, ADA_DM = f"iMessage;-;{OLA}", f"iMessage;-;{ADA}"
GROUP = "iMessage;+;chat610000000000000042"
NOON, NIGHT = london("2026-10-06 12:00"), london("2026-10-06 23:00")


def bb_send(n: int = 1):
    return respx.post(BB_SEND).respond(json={"status": 200, "message": "Message sent!",
                                             "data": {"guid": f"5C0FFEE0-SENT-{n:04d}"}})


def tg_send():
    return respx.post(TG_SEND).respond(json={"ok": True, "result": {"message_id": 9001}})


def ping(up: bool):
    if up:
        return respx.get(BB_PING).respond(json={"status": 200, "message": "Ping received!", "data": "pong"})
    return respx.get(BB_PING).respond(502, text="Bad Gateway")


def bodies(route) -> list[dict]:
    return [{k: v for k, v in json.loads(c.request.content).items() if k != "tempGuid"} for c in route.calls]


async def rows(sql, **params):
    async with tx() as conn:
        return await fetch_all(conn, sql, **params)


async def on_imessage(conn, *, telegram_id=None):
    """Ola, the admin, connected on iMessage first (so it is preferred), and maybe on Telegram too."""
    home = await seed_home(conn, telegram_id=None)
    await link(conn, home.ola, OLA, "imessage")
    if telegram_id:
        await link(conn, home.ola, telegram_id)
    return home


async def process(home, *script, **kwargs):
    return await inbound.process_household(home.id, LoopRuntime(FakeLLM(*script)), ADAPTERS,
                                           public_base_url="http://testserver", **kwargs)


async def tell(home, text="Bins tonight", *, now=None, **message):
    """Queue a send (to Ola unless told otherwise) and run the router once."""
    message.setdefault("target", "member")
    if message["target"] == "member":
        message.setdefault("member_id", home.ola)
    async with tx() as conn:
        await router.enqueue(conn, OutboundMessage(household_id=home.id, text=text, respect_quiet_hours=False,
                                                   **message), send_after=now)
    await router.dispatch_due(ADAPTERS, now=now)


def imessage() -> IMessageAdapter:
    adapter = ADAPTERS[Channel.imessage]
    assert isinstance(adapter, IMessageAdapter)
    return adapter


# ---------------------------------------------------------------- webhook
async def test_a_webhook_without_the_secret_is_refused_and_stores_nothing(client):
    async with tx() as conn:
        home = await on_imessage(conn)
    for secret in (None, "wrong", ""):
        assert (await post_imessage(client, bb_message(1, "we're out of eggs"), secret=secret)).status_code == 401
    assert await rows("select 1 from messages") == []

    assert (await post_imessage(client, bb_message(1, "we're out of eggs"))).status_code == 200
    assert (await post_imessage(client, bb_message(2, "and now from the bot itself", isFromMe=True))).status_code == 200
    assert await rows("select m.text, m.member_id, m.status, t.channel, t.external_thread_id, t.scope "
                      "from messages m join threads t on t.id = m.thread_id") == [
        {"text": "we're out of eggs", "member_id": home.ola, "status": "received", "channel": "imessage",
         "external_thread_id": OLA_DM, "scope": "dm"}]


async def test_strangers_on_imessage_are_invisible_and_an_invite_code_connects_the_sender(client):
    async with tx() as conn:
        home = await seed_home(conn)                                             # Ola, on Telegram only so far
        code = await members.create_invite(conn, home.ola, utcnow())
    await post_imessage(client, bb_message(1, "hello?"))
    await post_imessage(client, bb_message(2, "ZZZZ-9999"))
    await post_imessage(client, bb_message(3, code, group=GROUP))                # a code said in a group links nobody
    assert await rows("select 1 from messages") == [] and await rows("select 1 from outbox") == []
    assert len(await rows("select 1 from channel_identities")) == 1

    await post_imessage(client, bb_message(4, code))
    async with tx() as conn:
        identity = await fetch_one(
            conn, "select ci.handle, m.preferred_channel from channel_identities ci join members m "
                  "on m.id = ci.member_id where m.id = :m and ci.channel = 'imessage'", m=home.ola)
    assert identity == {"handle": OLA, "preferred_channel": "telegram"}           # the first channel stays preferred
    with respx.mock:
        sent = bb_send()
        await router.dispatch_due(ADAPTERS)
    assert bodies(sent) == [{"chatGuid": OLA_DM, "message": f"Hi Ola, you're connected. {inbound.WELCOME}",
                             "method": "apple-script"}]


# ---------------------------------------------------------------- turns
@respx.mock
async def test_an_imessage_turn_is_acked_and_a_replayed_webhook_changes_nothing(client):
    sent = bb_send()
    async with tx() as conn:
        home = await on_imessage(conn)
        await add_item(conn, home, "egg", location="fridge", staple=True, qty=6)
    payload = bb_message(501, "we're out of eggs")

    await post_imessage(client, payload)
    await post_imessage(client, payload)
    assert await process(home, call("log_inventory", changes=[{"item": "eggs", "action": "finished"}]), say("ACK")) == 1
    await router.dispatch_due(ADAPTERS)
    await post_imessage(client, payload)
    assert await process(home) == 0
    await router.dispatch_due(ADAPTERS)

    async with tx() as conn:
        assert await events_of(conn, home) == [("egg", "finished", None, "message")]
    assert len(await rows("select 1 from messages where direction = 'in'")) == 1
    assert len(await rows("select 1 from agent_actions")) == 1
    assert [r["status"] for r in await rows("select status from outbox")] == ["sent"]
    # Without the Private API there are no tapbacks: the tick goes as a message of its own.
    assert bodies(sent) == [{"chatGuid": OLA_DM, "message": "✅", "method": "apple-script"}]


@respx.mock
async def test_with_the_private_api_the_ack_is_a_tapback_on_the_message(client):
    react = respx.post(f"{BB}/message/react").respond(json={"status": 200, "message": "Reaction sent!", "data": {}})
    text = bb_send()
    ADAPTERS[Channel.imessage] = IMessageAdapter("http://mac-mini.test:1234", "test-bb-password", "test-bb-secret",
                                                 private_api=True)
    async with tx() as conn:
        home = await on_imessage(conn)
        await add_item(conn, home, "egg", staple=True, qty=6)
    await post_imessage(client, bb_message(501, "we're out of eggs"))
    await process(home, call("log_inventory", changes=[{"item": "eggs", "action": "finished"}]), say("ACK"))
    await router.dispatch_due(ADAPTERS)
    assert bodies(react) == [{"chatGuid": OLA_DM, "selectedMessageGuid": "5C0FFEE0-0501", "reaction": "like",
                              "partIndex": 0}]
    assert text.call_count == 0


@respx.mock
async def test_a_message_in_an_imessage_group_makes_it_the_family_chat_and_is_answered_there(client):
    sent = bb_send()
    async with tx() as conn:
        home = await on_imessage(conn)
        await add_member(conn, home, "Ada", imessage_id=ADA)
    await post_imessage(client, bb_message(1, "what's on the list?", handle=ADA, group=GROUP))
    await post_imessage(client, bb_message(2, "Liked “what's on the list?”", group=GROUP,
                                           associatedMessageGuid="p:0/5C0FFEE0-0001", associatedMessageType="like"))
    llm = FakeLLM(say("Nothing yet."))
    await inbound.process_household(home.id, LoopRuntime(llm), ADAPTERS)
    await router.dispatch_due(ADAPTERS)

    async with tx() as conn:
        thread = await fetch_one(
            conn, "select t.external_thread_id, t.scope, t.channel, t.id = h.primary_thread_id as is_primary "
                  "from threads t join households h on h.id = t.household_id "
                  "where h.id = :h and t.scope = 'group'", h=home.id)
    assert thread == {"external_thread_id": GROUP, "scope": "group", "channel": "imessage", "is_primary": True}
    assert llm.requests[0][1][-1].content[0].text == (
        'Ada: what\'s on the list?\nOla: [reacted \U0001F44D to: "what\'s on the list?"]')
    assert bodies(sent) == [{"chatGuid": GROUP, "message": "Nothing yet.", "method": "apple-script"}]


def one_second_caf() -> bytes:
    """A real CAF file, as an iPhone records a voice note in (a tone, not speech)."""
    caf = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-ar", "24000",
         "-f", "caf", "pipe:1"], check=True, capture_output=True).stdout
    assert caf.startswith(b"caff")
    return caf


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg, as the image has")
@respx.mock
async def test_a_caf_voice_note_is_downloaded_converted_to_16khz_wav_and_transcribed(client):
    caf = one_second_caf()
    heard = []

    class StandInTranscriber:
        async def transcribe(self, audio: bytes, mime: str) -> str:
            heard.append((audio, mime))
            return "we're out of eggs and bread"

    download = respx.get(f"{BB}/attachment/at_0_VOICE/download").respond(content=caf, content_type="audio/x-caf")
    bb_send()
    async with tx() as conn:
        home = await on_imessage(conn)
    await post_imessage(client, bb_message(1, "￼", attachments=[{
        "guid": "at_0_VOICE", "uti": "com.apple.coreaudio-format", "mimeType": None,
        "transferName": "Audio Message.caf", "totalBytes": len(caf)}]))
    llm = FakeLLM(say("ACK"))
    await inbound.process_household(home.id, LoopRuntime(llm), ADAPTERS, stt=StandInTranscriber())

    assert download.call_count == 1
    ((audio, mime),) = heard
    assert mime == "audio/wav"
    with wave.open(BytesIO(audio)) as wav:
        assert wav.getframerate() == 16000 and 0.9 < wav.getnframes() / 16000 < 1.1
    assert llm.requests[0][1][-1].content[0].text == "[voice note] we're out of eggs and bread"
    assert (await rows("select media from messages where direction = 'in'"))[0]["media"] == [
        {"kind": "audio", "mime": "audio/x-caf", "external_id": "at_0_VOICE",
         "transcript": "we're out of eggs and bread"}]


async def test_a_voice_note_that_cannot_be_converted_costs_only_its_transcript():
    """Not a CAF at all: ffmpeg fails, the turn goes on, and the agent is told what it lacks."""
    class Adapter:
        async def fetch_media(self, ref):
            return b"caff-but-not-really", "audio/x-caf"

    ref = {"kind": "audio", "external_id": "at_0_BROKEN"}
    assert await media_pipeline.prepare([ref], "h", "m", Adapter(), stt=object(), store=None) is False
    assert "transcript" not in ref


# ---------------------------------------------------------------- failures and the next channel
@respx.mock
async def test_a_send_the_mac_later_reports_as_failed_moves_to_telegram_once(client):
    bb_send(7)
    telegram = tg_send()
    async with tx() as conn:
        home = await on_imessage(conn, telegram_id="1001")
    await tell(home)
    assert telegram.call_count == 0

    failed = bb_message(0, "Bins tonight", kind="message-send-error", guid="5C0FFEE0-SENT-0007", isFromMe=True, error=22)
    stranger = bb_message(0, "x", kind="message-send-error", guid="5C0FFEE0-NOT-OURS", isFromMe=True, error=22)
    for payload in (failed, failed, stranger):
        assert (await post_imessage(client, payload)).status_code == 200
    await router.dispatch_due(ADAPTERS)

    assert await rows("select status, channel_used, last_error from outbox order by created_at") == [
        {"status": "failed", "channel_used": "imessage", "last_error": "iMessage error 22"},
        {"status": "sent", "channel_used": "telegram", "last_error": None}]
    assert [json.loads(c.request.content)["text"] for c in telegram.calls] == ["Bins tonight"]
    assert await rows("select 1 from messages where direction = 'in'") == []


@respx.mock
async def test_while_bluebubbles_is_down_a_members_dm_goes_to_their_next_channel_and_comes_back_after(client):
    imessage_sends, telegram = bb_send(), tg_send()
    async with tx() as conn:
        home = await on_imessage(conn, telegram_id="1001")                       # iMessage is Ola's preferred channel
    ping(up=True)
    assert await jobs.imessage_health(imessage(), NOON) == 0
    await tell(home, "one")
    assert (imessage_sends.call_count, telegram.call_count, imessage().degraded) == (1, 0, False)

    ping(up=False)
    await jobs.imessage_health(imessage(), NOON)
    assert imessage().degraded is True
    await tell(home, "two")
    assert imessage_sends.call_count == 1, "nothing is sent towards a server that is known to be down"
    assert json.loads(telegram.calls.last.request.content)["chat_id"] == "1001"
    assert await rows("select text, channel_used, status from outbox where text = 'two'") == [
        {"text": "two", "channel_used": "telegram", "status": "sent"}]
    # The member's own row was not touched: where their DMs go by choice is still iMessage.
    assert await rows("select preferred_channel from members where id = :m", m=home.ola) == [
        {"preferred_channel": "imessage"}]

    ping(up=True)
    await jobs.imessage_health(imessage(), NOON)
    await tell(home, "three")
    assert imessage().degraded is False and bodies(imessage_sends)[-1]["message"] == "three"


@respx.mock
async def test_a_reply_waiting_for_an_imessage_dm_follows_its_owner_but_an_ack_and_a_group_stay(client):
    imessage_sends, telegram = bb_send(), tg_send()
    async with tx() as conn:
        home = await on_imessage(conn, telegram_id="1001")
        await add_member(conn, home, "Ada", imessage_id=ADA)                     # Ada has no other channel
    await post_imessage(client, bb_message(1, "what's on the list?"))
    await post_imessage(client, bb_message(2, "hello all", handle=ADA, group=GROUP))
    (dm,) = await rows("select id from threads where scope = 'dm'")
    (group,) = await rows("select id from threads where scope = 'group'")
    (asked,) = await rows("select id from messages where text = 'what''s on the list?'")
    imessage().degraded = True

    await tell(home, "Eggs and bread.", target="thread", thread_id=dm["id"])
    assert [json.loads(c.request.content)["text"] for c in telegram.calls] == ["Eggs and bread."]
    assert imessage_sends.call_count == 0

    # An ack would mean nothing in another chat, a group has no other channel, and Ada has only iMessage:
    # those are still tried where they belong, and retried until the Mac is back.
    await tell(home, None, target="thread", thread_id=dm["id"], react_emoji="ack", reply_to_message_id=asked["id"])
    await tell(home, "Dinner at 7", target="thread", thread_id=group["id"])
    await tell(home, "Your turn for the bins", member_id=home.members["Ada"])
    assert telegram.call_count == 1
    assert bodies(imessage_sends) == [
        {"chatGuid": OLA_DM, "message": "✅", "method": "apple-script"},
        {"chatGuid": GROUP, "message": "Dinner at 7", "method": "apple-script"},
        {"chatGuid": ADA_DM, "message": "Your turn for the bins", "method": "apple-script"}]


@respx.mock
async def test_a_household_message_goes_to_each_adult_while_the_imessage_group_cannot_be_reached(client):
    imessage_sends, telegram = bb_send(), tg_send()
    async with tx() as conn:
        home = await on_imessage(conn, telegram_id="1001")
        await add_member(conn, home, "Ada", imessage_id=ADA, telegram_id="1002")
        await add_member(conn, home, "Tobi", role="child")
    await post_imessage(client, bb_message(1, "hello all", group=GROUP))          # the group becomes the family chat
    imessage().degraded = True
    await tell(home, "Today: Chatterbox 09:00", target="household", dedupe_key="daily_brief:2026-10-06")
    await router.dispatch_due(ADAPTERS)
    assert imessage_sends.call_count == 0
    assert sorted((b["chat_id"], b["text"]) for b in (json.loads(c.request.content) for c in telegram.calls)) == [
        ("1001", "Today: Chatterbox 09:00"), ("1002", "Today: Chatterbox 09:00")]

    imessage().degraded = False
    await tell(home, "Today: nothing", target="household")
    assert bodies(imessage_sends) == [{"chatGuid": GROUP, "message": "Today: nothing", "method": "apple-script"}]
    assert telegram.call_count == 2


@respx.mock
async def test_a_member_with_only_imessage_is_retried_there_and_never_sent_elsewhere(client):
    down = respx.post(BB_SEND).mock(side_effect=httpx.ConnectError("refused"))
    telegram = tg_send()
    async with tx() as conn:
        home = await on_imessage(conn)                                           # no other channel
        await add_member(conn, home, "Ada", telegram_id="1002")
    imessage().degraded = True
    await tell(home, "Bins tonight", now=NOON)
    assert (down.call_count, telegram.call_count) == (1, 0)
    assert await rows("select status, attempts, channel_used, send_after from outbox") == [
        {"status": "pending", "attempts": 1, "channel_used": "imessage", "send_after": NOON + timedelta(seconds=10)}]


# ---------------------------------------------------------------- telling the admin
@respx.mock
async def test_the_admin_is_told_once_per_outage_on_a_channel_that_works(client):
    telegram = tg_send()
    async with tx() as conn:
        home = await on_imessage(conn, telegram_id="1001")
        await add_member(conn, home, "Ada", imessage_id=ADA)
        other = await seed_home(conn, telegram_id="2001")                        # a household that never used iMessage

    ping(up=False)
    assert await jobs.imessage_health(imessage(), NOON) == 1
    assert await jobs.imessage_health(imessage(), NOON + timedelta(minutes=5)) == 0
    assert await jobs.imessage_health(imessage(), NOON + timedelta(minutes=10)) == 0
    await router.dispatch_due(ADAPTERS, now=NOON + timedelta(minutes=10))
    warnings = await rows("select household_id, target, member_id, text, status, channel_used from outbox")
    assert warnings == [{"household_id": home.id, "target": "member", "member_id": home.ola,
                         "text": jobs.IMESSAGE_DOWN, "status": "sent", "channel_used": "telegram"}]
    assert [json.loads(c.request.content)["chat_id"] for c in telegram.calls] == ["1001"]
    assert await rows("select 1 from outbox where household_id = :h", h=other.id) == []

    ping(up=True)
    assert await jobs.imessage_health(imessage(), NOON + timedelta(minutes=15)) == 0
    assert await rows("select 1 from nudge_log") == []
    ping(up=False)
    assert await jobs.imessage_health(imessage(), NOON + timedelta(hours=3)) == 1   # a new outage, a new warning
    assert len(await rows("select 1 from outbox")) == 2


@respx.mock
async def test_a_warning_still_held_for_quiet_hours_is_dropped_when_the_outage_ends(client):
    telegram = tg_send()
    async with tx() as conn:
        home = await on_imessage(conn, telegram_id="1001")
        await execute(conn, "update members set quiet_start = '21:30', quiet_end = '07:00' where id = :m", m=home.ola)
    ping(up=False)
    assert await jobs.imessage_health(imessage(), NIGHT) == 1
    await router.dispatch_due(ADAPTERS, now=NIGHT)
    assert await rows("select status, send_after from outbox") == [
        {"status": "pending", "send_after": london("2026-10-07 07:00")}]

    ping(up=True)
    await jobs.imessage_health(imessage(), NIGHT + timedelta(minutes=5))
    await router.dispatch_due(ADAPTERS, now=london("2026-10-07 07:00"))
    assert await rows("select status from outbox") == [{"status": "cancelled"}]
    assert telegram.call_count == 0
