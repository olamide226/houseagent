"""Inbound pipeline: webhook persistence, idempotency, debounce, the turn and its response policy."""
import json
from datetime import timedelta

import pytest
import respx

from app.agent.loop import LoopRuntime
from app.channels.base import ADAPTERS
from app.core.envelope import Channel
from app.db import execute, fetch_all, fetch_one, fetch_val, tx
from app.pipeline import inbound, router
from app.pipeline.inbound import SORRY, build_envelope, group_by_thread
from tests.helpers import FakeAdapter, FakeLLM, add_item, add_member, call, events_of, say, seed_home, tg_update

SECRET = {"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"}
TG = "https://api.telegram.org/bot424242:TEST-TOKEN"
GROUP = -1001234567890


async def post(client, update, headers=SECRET):
    return await client.post("/webhooks/telegram", json=update, headers=headers)


async def process(home, *script, adapters=None, stt=None):
    return await inbound.process_household(home.id, LoopRuntime(FakeLLM(*script)), adapters or {},
                                           stt=stt, public_base_url="http://testserver")


async def rows(sql, **params):
    async with tx() as conn:
        return await fetch_all(conn, sql, **params)


# ---------------------------------------------------------------- webhook
async def test_webhook_rejects_a_wrong_secret_and_unknown_channels(client):
    assert (await post(client, tg_update(1, "hi"), headers={})).status_code == 401
    assert (await post(client, tg_update(1, "hi"), headers={"X-Telegram-Bot-Api-Secret-Token": "x"})).status_code == 401
    ADAPTERS.pop(Channel.imessage)   # a channel that is not set up in this deployment
    assert (await client.post("/webhooks/imessage?secret=test-bb-secret", json={})).status_code == 404
    assert await rows("select 1 from messages") == []


async def test_webhook_stores_a_known_senders_message_as_received(client):
    async with tx() as conn:
        home = await seed_home(conn)
    assert (await post(client, tg_update(501, "we're out of eggs"))).status_code == 200
    (message,) = await rows("select m.*, t.channel, t.external_thread_id, t.scope from messages m "
                            "join threads t on t.id = m.thread_id")
    assert (message["household_id"], message["member_id"]) == (home.id, home.ola)
    assert (message["direction"], message["status"], message["text"]) == ("in", "received", "we're out of eggs")
    assert (message["channel"], message["external_thread_id"], message["scope"]) == ("telegram", "1001", "dm")


async def test_unknown_senders_are_invisible(client):
    async with tx() as conn:
        await seed_home(conn)
    assert (await post(client, tg_update(1, "hello?", user_id=666, name="Stranger"))).status_code == 200
    assert (await post(client, tg_update(2, "ZZZZ-9999", user_id=666, name="Stranger"))).status_code == 200
    for table in ("messages", "threads", "outbox"):
        assert await rows(f"select 1 from {table}") == []


async def test_a_malformed_payload_is_answered_200_and_stores_nothing(client):
    response = await client.post("/webhooks/telegram", content=b"{not json", headers=SECRET)
    assert response.status_code == 200
    assert await rows("select 1 from messages") == []


async def test_first_group_message_from_a_member_sets_the_primary_thread(client):
    async with tx() as conn:
        home = await seed_home(conn)
    await post(client, tg_update(1, "hello family", chat_id=GROUP, chat_type="supergroup"))
    async with tx() as conn:
        primary = await fetch_val(conn, "select primary_thread_id from households where id = :h", h=home.id)
        thread = await fetch_one(conn, "select id, scope from threads where external_thread_id = :e", e=str(GROUP))
    assert thread["scope"] == "group" and primary == thread["id"]


async def test_invite_code_connects_the_sender_and_queues_a_welcome(client):
    from app.core.timeutil import utcnow
    from app.services import members
    async with tx() as conn:
        home = await seed_home(conn, telegram_id=None)
        code = await members.create_invite(conn, home.ola, utcnow())
    await post(client, tg_update(1, f"/start {code}", user_id=1001))
    identity, = await rows("select member_id, channel, handle from channel_identities")
    assert (identity["member_id"], identity["channel"], identity["handle"]) == (home.ola, "telegram", "1001")
    (welcome,) = await rows("select o.text, t.external_thread_id from outbox o join threads t on t.id = o.thread_id")
    assert welcome == {"text": f"Hi Ola, you're connected. {inbound.WELCOME}", "external_thread_id": "1001"}
    assert await rows("select 1 from messages") == []          # the code itself is never a turn

    adapter = FakeAdapter()
    await router.dispatch_due({Channel.telegram: adapter})
    assert adapter.sent == [("1001", f"Hi Ola, you're connected. {inbound.WELCOME}", None)]


async def test_invite_attempts_are_rate_limited_per_handle(client):
    from app.core.timeutil import utcnow
    from app.services import members
    async with tx() as conn:
        home = await seed_home(conn, telegram_id=None)
        code = await members.create_invite(conn, home.ola, utcnow())
    for attempt in range(5):
        await post(client, tg_update(attempt, "AAAA-2222", user_id=1001))
    await post(client, tg_update(99, code, user_id=1001))      # sixth attempt within the hour: refused
    assert await rows("select 1 from channel_identities") == []


# ---------------------------------------------------------------- idempotency (acceptance item)
@respx.mock
async def test_replaying_a_webhook_creates_no_duplicate_rows_or_sends(client):
    reaction = respx.post(f"{TG}/setMessageReaction").respond(json={"ok": True, "result": True})
    send = respx.post(f"{TG}/sendMessage").respond(json={"ok": True, "result": {"message_id": 9001}})
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "egg", location="fridge", staple=True, qty=6)
    update = tg_update(501, "we're out of eggs")

    await post(client, update)
    await post(client, update)                                  # provider retry before processing
    assert len(await rows("select 1 from messages")) == 1

    turns = await process(home, call("log_inventory", changes=[{"item": "eggs", "action": "finished"}]), say("ACK"),
                          adapters=ADAPTERS)
    assert turns == 1
    await router.dispatch_due(ADAPTERS)

    await post(client, update)                                  # provider retry after processing
    assert await process(home, adapters=ADAPTERS) == 0          # nothing waiting: the FakeLLM is never asked
    await router.dispatch_due(ADAPTERS)

    async with tx() as conn:
        assert await events_of(conn, home) == [("egg", "finished", None, "message")]
    assert len(await rows("select 1 from messages where direction = 'in'")) == 1
    assert len(await rows("select 1 from shopping_list_items")) == 1
    assert len(await rows("select 1 from agent_actions")) == 1
    assert [r["status"] for r in await rows("select status from outbox")] == ["sent"]
    assert reaction.call_count == 1 and send.call_count == 0
    assert json.loads(reaction.calls.last.request.content) == {
        "chat_id": "1001", "message_id": 501, "reaction": [{"type": "emoji", "emoji": "\U0001F44D"}]}


# ---------------------------------------------------------------- debounce
async def test_a_household_is_ready_only_after_its_newest_message_has_settled(client):
    async with tx() as conn:
        home = await seed_home(conn)
    await post(client, tg_update(1, "out of eggs"))
    await post(client, tg_update(2, "and bread"))
    async with tx() as conn:
        assert await inbound.ready_households(conn, 4) == []
        assert 0 < await inbound.seconds_until_ready(conn, 4) <= 4
        await execute(conn, "update messages set created_at = created_at - cast(:age as interval) where external_id = '1'",
                      age=timedelta(seconds=10))
        assert await inbound.ready_households(conn, 4) == []    # the newest message is still fresh
        await execute(conn, "update messages set created_at = created_at - cast(:age as interval) where external_id = '2'",
                      age=timedelta(seconds=5))
        assert await inbound.ready_households(conn, 4) == [home.id]
        assert await inbound.seconds_until_ready(conn, 4) == 0
        await execute(conn, "update messages set status = 'processed'")
        assert await inbound.ready_households(conn, 4) == []
        assert await inbound.seconds_until_ready(conn, 4) is None


def stored(message_id, thread, text, name="Ola", scope="dm", **extra):
    return {"id": message_id, "household_id": "h", "thread_id": thread, "member_id": name.lower(), "text": text,
            "media": [], "meta": {}, "created_at": "2026-10-05T20:00:00Z", "channel": "telegram", "scope": scope,
            "member_name": name, **extra}


def test_messages_group_into_one_batch_per_thread_oldest_first():
    batches = group_by_thread([
        stored("1", "dm", "out of eggs"), stored("2", "group", "anyone home?", scope="group"),
        stored("3", "dm", "and bread"), stored("4", "dm", "oh and milk")])
    assert [[m["id"] for m in batch] for batch in batches] == [["1", "3", "4"], ["2"]]
    envelope = build_envelope(batches[0])
    assert envelope.text == "out of eggs\nand bread\noh and milk"
    assert envelope.message_ids == ["1", "3", "4"] and envelope.thread_id == "dm"


def test_envelope_text_covers_voice_photo_location_reactions_and_group_names():
    batch = [
        stored("1", "g", None, "Ada", "group", media=[{"kind": "audio", "transcript": "we need rice"}]),
        stored("2", "g", "Tesco receipt", "Ola", "group", media=[{"kind": "image", "external_id": "F"}]),
        stored("3", "g", None, "Ola", "group", media=[{"kind": "location", "lat": 51.5, "lng": -0.12}]),
        stored("4", "g", None, "Ada", "group", meta={"reaction_emoji": "✅", "reaction_target_external_id": "777"}),
        stored("5", "g", None, "Ada", "group", media=[{"kind": "audio"}]),
    ]
    envelope = build_envelope(batch, {"777": "Probably running low: milk. Add to the list?"})
    assert envelope.text.splitlines() == [
        "Ada: [voice note] we need rice",
        "Ola: Tesco receipt",
        "Ola: [photo]",
        "Ola: [location] 51.5, -0.12",
        'Ada: [reacted ✅ to: "Probably running low: milk. Add to the list?"]',
        "Ada: [voice note] (could not be transcribed)",
    ]
    assert (envelope.member_name, envelope.scope, len(envelope.images)) == ("Ada", "group", 1)


async def test_rapid_messages_become_one_turn_answered_as_a_reply_to_the_last(client):
    async with tx() as conn:
        home = await seed_home(conn)
    for n, text in enumerate(["do we have eggs", "and bread", "oh and milk"], start=1):
        await post(client, tg_update(n, text))
    llm = FakeLLM(say("No eggs, bread or milk recorded."))
    turns = await inbound.process_household(home.id, LoopRuntime(llm), {})
    assert turns == 1 and len(llm.requests) == 1
    assert llm.requests[0][1][-1].content[0].text == "do we have eggs\nand bread\noh and milk"
    (reply,) = await rows("select o.text, m.external_id as reply_to from outbox o "
                          "join messages m on m.id = o.reply_to_message_id")
    assert reply == {"text": "No eggs, bread or milk recorded.", "reply_to": "3"}
    assert {r["status"] for r in await rows("select status from messages")} == {"processed"}


# ---------------------------------------------------------------- response policy and failures
async def test_noop_sends_nothing_and_a_single_message_reply_is_not_threaded(client):
    async with tx() as conn:
        home = await seed_home(conn)
    await post(client, tg_update(1, "love you, see you at 6", chat_id=GROUP, chat_type="supergroup"))
    await process(home, say("NOOP"))
    assert await rows("select 1 from outbox") == []

    await post(client, tg_update(2, "do we have rice?"))
    await process(home, say("No rice recorded."))
    (reply,) = await rows("select text, reply_to_message_id, respect_quiet_hours from outbox")
    assert reply == {"text": "No rice recorded.", "reply_to_message_id": None, "respect_quiet_hours": False}


async def test_turn_usage_tools_and_latency_are_stored_on_the_message(client):
    async with tx() as conn:
        home = await seed_home(conn)
    await post(client, tg_update(1, "out of rice"))
    await process(home, call("log_inventory", changes=[{"item": "rice", "action": "finished"}]), say("ACK"))
    (message,) = await rows("select meta, status, processed_at from messages")
    turn = message["meta"]["turn"]
    assert message["meta"]["usage"] == {"input_tokens": 200, "output_tokens": 25, "cached_tokens": 0}
    assert turn["outcome"] == "ack" and turn["latency_ms"] >= 0
    assert [(c["name"], c["is_error"]) for c in turn["tool_calls"]] == [("log_inventory", False)]
    assert message["status"] == "processed" and message["processed_at"] is not None
    (action,) = await rows("select message_id, member_id, source from agent_actions")
    assert action["source"] == "agent" and action["member_id"] == home.ola


async def test_a_failed_turn_records_nothing_marks_the_messages_failed_and_apologises_once(client):
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "rice", qty=3)
    await post(client, tg_update(1, "used some rice"))
    await post(client, tg_update(2, "about a cup"))
    # One tool call succeeds, then the model call fails: the whole turn is rolled back.
    await process(home, call("log_inventory", changes=[{"item": "rice", "action": "used", "quantity": 1}]))

    statuses = await rows("select status, meta from messages order by external_id")
    assert [m["status"] for m in statuses] == ["failed", "failed"]
    assert "ran out of scripted responses" in statuses[0]["meta"]["error"]
    assert [r["text"] for r in await rows("select text from outbox")] == [SORRY]
    assert await rows("select 1 from inventory_events") == []
    assert await process(home) == 0                              # a failed turn is not retried forever


async def test_the_dashboard_keyword_gets_a_login_link_by_dm_without_an_agent_turn(client):
    async with tx() as conn:
        home = await seed_home(conn)
    await post(client, tg_update(1, " Dashboard ", chat_id=GROUP, chat_type="supergroup"))
    assert await process(home) == 0                              # no scripted LLM responses were needed
    (link,) = await rows("select target, member_id, text from outbox")
    assert (link["target"], link["member_id"]) == ("member", home.ola)
    token = link["text"].rsplit("/login/", 1)[1]
    assert link["text"].startswith("Your dashboard link") and "http://testserver/login/" in link["text"]
    assert (await rows("select token_hash from login_tokens"))[0]["token_hash"] != token   # stored hashed

    adapter = FakeAdapter()
    await router.dispatch_due({Channel.telegram: adapter})
    assert [chat for chat, _, _ in adapter.sent] == ["1001"]     # the member's own DM, not the group


async def test_voice_notes_are_transcribed_before_the_turn(client):
    class StandInTranscriber:
        async def transcribe(self, audio: bytes, mime: str) -> str:
            assert (audio, mime) == (b"audio-bytes", "audio/ogg")
            return "we're out of eggs and bread"

    async with tx() as conn:
        home = await seed_home(conn)
    await post(client, tg_update(1, voice={"file_id": "V1", "mime_type": "audio/ogg", "duration": 3}))
    llm = FakeLLM(say("NOOP"))
    await inbound.process_household(home.id, LoopRuntime(llm), {Channel.telegram: FakeAdapter()},
                                    stt=StandInTranscriber())
    assert llm.requests[0][1][-1].content[0].text == "[voice note] we're out of eggs and bread"
    (message,) = await rows("select media from messages")
    assert message["media"][0]["transcript"] == "we're out of eggs and bread"


async def test_two_members_in_the_group_are_one_turn_with_names(client):
    async with tx() as conn:
        home = await seed_home(conn)
        await add_member(conn, home, "Ada", telegram_id="1002")
    await post(client, tg_update(1, "we're out of rice", chat_id=GROUP, chat_type="supergroup"))
    await post(client, tg_update(2, "and yam", user_id=1002, name="Ada", chat_id=GROUP, chat_type="supergroup"))
    llm = FakeLLM(say("NOOP"))
    await inbound.process_household(home.id, LoopRuntime(llm), {})
    assert llm.requests[0][1][-1].content[0].text == "Ola: we're out of rice\nAda: and yam"
    assert "Speaking: Ada (telegram, group)" in llm.requests[0][0]


@pytest.mark.parametrize("apply", [False, True])
async def test_simulate_turn_runs_the_same_turn_without_a_channel(apply):
    from app.db import engine
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "rice", staple=True, qty=3)
    runtime = LoopRuntime(FakeLLM(call("log_inventory", changes=[{"item": "rice", "action": "finished"}]), say("ACK")))
    async with engine().connect() as conn:
        transaction = await conn.begin()
        result = await inbound.simulate_turn(conn, runtime, home.id, home.ola, "finished the rice")
        await (transaction.commit() if apply else transaction.rollback())

    assert result.ack_only and len(result.tool_calls) == 1
    assert len(await rows("select 1 from inventory_events")) == (1 if apply else 0)
    assert len(await rows("select 1 from shopping_list_items")) == (1 if apply else 0)
    outbox = await rows("select status from outbox")
    assert outbox == ([{"status": "simulated"}] if apply else [])
    adapter = FakeAdapter()
    await router.dispatch_due({Channel.telegram: adapter})       # simulated sends never leave
    assert adapter.sent == [] and adapter.reactions == []
