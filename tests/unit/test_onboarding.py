"""Onboarding and the family (spec section 12): invites end to end, adding people from chat,
the setup steps, and the settings that are saved by talking."""
import json
import re
from datetime import time, timedelta

import pytest

from app.agent import actions
from app.agent.base import ToolError
from app.agent.loop import LoopRuntime
from app.agent.prompt import STEP_GUIDE
from app.agent.tools import run_tool
from app.core.envelope import Channel
from app.core.timeutil import parse_clock, utcnow
from app.db import execute, fetch_all, fetch_one, fetch_val, tx
from app.pipeline import inbound, router
from app.services import households, members
from tests.helpers import (
    FakeAdapter,
    FakeLLM,
    active_list,
    add_item,
    add_member,
    call,
    ctx_for,
    link,
    post_whatsapp,
    say,
    seed_home,
    tg_update,
    wa_message,
)

SECRET = {"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"}
CODE = re.compile(r"[A-Z]{4}-[A-Z0-9]{4}")


async def post(client, update):
    return await client.post("/webhooks/telegram", json=update, headers=SECRET)


async def rows(sql, **params):
    async with tx() as conn:
        return await fetch_all(conn, sql, **params)


async def tool(home, tool_name, *, speaker="Ola", **args):
    async with tx() as conn:
        return await run_tool(tool_name, args, ctx_for(conn, home, speaker))


async def deliver():
    adapter = FakeAdapter()
    await router.dispatch_due({Channel.telegram: adapter})
    return adapter.sent


# ---------------------------------------------------------------- invites, end to end
async def test_the_first_adult_to_connect_is_asked_the_first_question_and_later_adults_are_welcomed(client):
    async with tx() as conn:
        home = await seed_home(conn, telegram_id=None, onboarding=True)
        ada = await add_member(conn, home, "Ada")
        ola_code = await members.create_invite(conn, home.ola, utcnow())
        ada_code = await members.create_invite(conn, ada, utcnow())

    await post(client, tg_update(1, f"/start {ola_code}", user_id=1001))
    assert await deliver() == [("1001", f"Hi Ola, you're connected. {inbound.FIRST_QUESTION}", None)]

    await tool(home, "onboarding_advance", step="family")
    await post(client, tg_update(2, f"/start {ada_code}", user_id=1002, name="Ada"))
    assert await deliver() == [("1002", f"Hi Ada, you're connected. {inbound.WELCOME}", None)]


async def test_a_second_adult_added_from_chat_redeems_the_invite_and_is_linked(client):
    async with tx() as conn:
        home = await seed_home(conn)
    await post(client, tg_update(1, "my wife Ada lives here too, add her"))
    llm = FakeLLM(call("add_family_member", name="Ada", role="adult"), say("Added Ada. Pass her the invite above."))
    await inbound.process_household(home.id, LoopRuntime(llm), {})

    # The invite goes to the person who asked, as its own message, before the model's reply.
    sent = await deliver()
    assert [chat for chat, _, _ in sent] == ["1001", "1001"] and sent[1][1] == "Added Ada. Pass her the invite above."
    (code,) = set(CODE.findall(sent[0][1]))                 # once in the link, once on its own
    assert f"https://t.me/home_test_bot?start={code}" in sent[0][1]
    # The model never saw the code, and only its hash is stored on the member.
    assert code not in str(llm.requests[1][1][-1].content)
    (ada,) = await rows("select id, role, invite_code_hash, preferred_channel from members where name = 'Ada'")
    assert ada["role"] == "adult" and ada["invite_code_hash"] not in (None, code) and ada["preferred_channel"] is None

    # Ada taps the link. Her chat is linked, becomes her preferred channel, and she is welcomed.
    await post(client, tg_update(2, f"/start {code}", user_id=1002, name="Ada"))
    assert await rows("select member_id, channel, handle from channel_identities where handle = '1002'") == [
        {"member_id": ada["id"], "channel": "telegram", "handle": "1002"}]
    assert await rows("select preferred_channel from members where name = 'Ada'") == [{"preferred_channel": "telegram"}]
    assert await deliver() == [("1002", f"Hi Ada, you're connected. {inbound.WELCOME}", None)]

    # From then on her messages are hers, and sends to her reach her chat.
    await post(client, tg_update(3, "we're out of rice", user_id=1002, name="Ada"))
    assert await rows("select member_id from messages where text = 'we''re out of rice'") == [{"member_id": ada["id"]}]
    reader = FakeLLM(say("NOOP"))
    await inbound.process_household(home.id, LoopRuntime(reader), {})
    assert "Speaking: Ada (telegram, dm)" in reader.requests[0][0]


async def test_an_invite_cannot_be_reused_by_someone_else_and_stops_working_when_expired_or_revoked(client):
    async with tx() as conn:
        home = await seed_home(conn)
        ada = await add_member(conn, home, "Ada")
        grace = await add_member(conn, home, "Grace")
        ada_code = await members.create_invite(conn, ada, utcnow())
        old_code = await members.create_invite(conn, grace, utcnow() - timedelta(days=7, minutes=1))

    await post(client, tg_update(1, ada_code, user_id=1002, name="Ada"))
    await post(client, tg_update(2, ada_code, user_id=666, name="Stranger"))        # a forwarded code, reused
    await post(client, tg_update(3, old_code, user_id=1003, name="Grace"))          # eight days old
    async with tx() as conn:
        fresh = await members.invite(actions.Recorder(ctx_for(conn, home)), grace)
        await members.revoke_invite(actions.Recorder(ctx_for(conn, home)), grace)
    await post(client, tg_update(4, fresh, user_id=1003, name="Grace"))             # revoked on the Family page

    assert await rows("select member_id, handle from channel_identities where handle <> '1001'") == [
        {"member_id": ada, "handle": "1002"}]
    assert [chat for chat, _, _ in await deliver()] == ["1002"]                      # nobody else heard a word
    assert await rows("select 1 from messages where direction = 'in'") == []


async def test_a_child_is_added_with_no_invite_and_can_never_have_one():
    async with tx() as conn:
        home = await seed_home(conn)
    result, is_error = await tool(home, "add_family_member", name="Tobi")
    assert not is_error and result == "NEW: Tobi (child)"
    (tobi,) = await rows("select id, role, invite_code_hash, invite_expires_at from members where name = 'Tobi'")
    assert (tobi["role"], tobi["invite_code_hash"], tobi["invite_expires_at"]) == ("child", None, None)
    assert await rows("select 1 from outbox") == []
    async with tx() as conn:
        with pytest.raises(ToolError):
            await members.invite(actions.Recorder(ctx_for(conn, home)), tobi["id"])
        with pytest.raises(ToolError):
            await members.set_quiet_hours(actions.Recorder(ctx_for(conn, home)), [tobi["id"]], time(20), time(7))
        assert await members.create_login_token(conn, tobi["id"], utcnow()) is None


async def test_adding_someone_twice_keeps_one_record_and_reinvites_only_an_unconnected_adult():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_member(conn, home, "Ada", telegram_id="1002")
    await tool(home, "add_family_member", name="Grace", role="adult")
    (first,) = await rows("select invite_code_hash from members where name = 'Grace'")
    await tool(home, "add_family_member", name=" grace ", role="adult")       # asked again: a fresh invite
    await tool(home, "add_family_member", name="ada", role="adult")           # already connected: nothing to send
    await tool(home, "add_family_member", name="Tobi")
    await tool(home, "add_family_member", name="tobi")

    assert [r["name"] for r in await rows("select name from members order by created_at")] == [
        "Ola", "Ada", "Grace", "Tobi"]
    (second,) = await rows("select invite_code_hash from members where name = 'Grace'")
    assert second != first
    invites = await rows("select member_id, text from outbox order by created_at")
    assert [i["member_id"] for i in invites] == [home.ola, home.ola] and all("Grace" in i["text"] for i in invites)


async def test_undo_removes_a_person_just_added_but_not_once_they_have_connected(client):
    async with tx() as conn:
        home = await seed_home(conn)
    await tool(home, "add_family_member", name="Tobi")
    result, _ = await tool(home, "undo_last")
    assert result.startswith("OK: undid add_family_member")
    assert await rows("select 1 from members where name = 'Tobi'") == []

    await tool(home, "add_family_member", name="Ada", role="adult")
    (invite,) = await rows("select text from outbox")
    await post(client, tg_update(1, CODE.search(invite["text"]).group(), user_id=1002, name="Ada"))
    result, is_error = await tool(home, "undo_last")
    assert is_error and "invite.redeem" in result
    assert len(await rows("select 1 from channel_identities where handle = '1002'")) == 1


# ---------------------------------------------------------------- the setup conversation
async def test_the_setup_tool_and_prompt_are_offered_only_while_setup_is_open():
    async with tx() as conn:
        home = await seed_home(conn, onboarding=True)

    async def offered():
        llm = FakeLLM(say("NOOP"))
        async with tx() as conn:
            await inbound.simulate_turn(conn, LoopRuntime(llm), home.id, home.ola, "hello")
        system, _, tools = llm.requests[0]
        return system, {t.name for t in tools}

    system, tools = await offered()
    assert "onboarding_advance" in tools
    assert "Current step: family. Remaining: family, routines, shops, staples, tour, rhythm, presence." in system
    assert STEP_GUIDE["family"] in system

    for step in ("family", "routines", "shops", "staples", "tour"):
        await tool(home, "onboarding_advance", step=step)
    system, tools = await offered()
    assert "Current step: rhythm. Remaining: rhythm, presence." in system and STEP_GUIDE["rhythm"] in system

    result, _ = await tool(home, "onboarding_advance", step="rhythm")
    assert "complete" in result
    system, tools = await offered()
    assert "onboarding_advance" not in tools and "ONBOARDING" not in system
    result, is_error = await tool(home, "onboarding_advance", step="family")   # a stale call cannot reopen it
    assert is_error
    async with tx() as conn:
        assert (await households.onboarding(conn, home.id))["step"] is None


async def test_steps_can_be_skipped_or_done_out_of_order_and_undo_reopens_one():
    async with tx() as conn:
        home = await seed_home(conn, onboarding=True)

    async def state():
        async with tx() as conn:
            return await fetch_val(conn, "select onboarding_state from households where id = :h", h=home.id)

    result, _ = await tool(home, "onboarding_advance", step="family")
    assert result == "OK: family done. Next step: routines"
    await tool(home, "onboarding_advance", step="staples", skipped=True)      # answered out of turn
    await tool(home, "onboarding_advance", step="family")                     # said twice
    assert await state() == {"step": "routines", "done": ["family", "staples"], "skipped": ["staples"]}

    result, _ = await tool(home, "onboarding_advance", step="routines", skipped=True)
    assert result == "OK: routines skipped. Next step: shops"
    await tool(home, "undo_last")
    assert (await state())["step"] == "routines"
    _, is_error = await tool(home, "onboarding_advance", step="garden")
    assert is_error


# ---------------------------------------------------------------- the presence step
URL = re.compile(r"http://testserver/presence/([\w-]{43})")


async def presence_messages():
    """Queued presence DMs as {member name: (token, respect_quiet_hours)}."""
    sent = await rows("select m.name, o.text, o.target, o.respect_quiet_hours from outbox o "
                      "join members m on m.id = o.member_id where o.text like '%/presence/%' order by o.created_at")
    assert all(row["target"] == "member" for row in sent)
    return {row["name"]: (URL.search(row["text"]).group(1), row["respect_quiet_hours"]) for row in sent}


async def setting_up(conn, *, reached="rhythm"):
    """Ola and Ada connected, Grace invited but not connected, a child, and setup at `reached`."""
    home = await seed_home(conn, onboarding=True)
    await add_member(conn, home, "Ada", telegram_id="1002")
    await add_member(conn, home, "Grace")
    await add_member(conn, home, "Tobi", role="child")
    steps = households.ONBOARDING_STEPS
    await execute(conn, "update households set onboarding_state = cast(:state as jsonb)",
                  state=json.dumps({"step": reached, "done": list(steps[:steps.index(reached)])}))
    return home


async def test_answering_the_last_question_sends_each_connected_adult_a_personal_link_that_works(client):
    async with tx() as conn:
        home = await setting_up(conn)
    await tool(home, "remember", key="shops", value="Tesco Extra, Costco")

    result, is_error = await tool(home, "onboarding_advance", step="rhythm")
    assert not is_error and result.splitlines()[0] == "OK: rhythm done. Setup is complete"
    assert "NOTE: Ada, Ola got a private message with a personal link" in result   # made together: by name
    assert await fetch_state(home) == {"step": None, "skipped": [],
                                       "done": ["family", "routines", "shops", "staples", "tour", "rhythm", "presence"]}

    links = await presence_messages()
    assert set(links) == {"Ola", "Ada"} and links["Ola"][0] != links["Ada"][0]
    assert (links["Ola"][1], links["Ada"][1]) == (False, True)       # Ola is talking to us now; Ada may be asleep
    (text,) = {r["text"].replace(links["Ola"][0], "TOKEN") for r in await rows(
        "select text from outbox where member_id = :m", m=home.ola)}
    assert text == households.presence_text("http://testserver/presence/TOKEN", ["Costco", "Tesco Extra"])
    for wanted in ("Arrive", "run without asking", "Get Contents of URL", "POST", "event = enter", "event = exit",
                   "place = Home", "Shops I know: Costco, Tesco Extra."):
        assert wanted in text
    # The model is told that links went out, never what they are; only hashes are stored.
    logged = json.dumps(await rows("select args, result, inverse from agent_actions"), default=str)
    hashes = {r["name"]: r["presence_token_hash"] for r in await rows("select name, presence_token_hash from members")}
    for token, _ in links.values():
        assert token not in result and token not in logged and token not in hashes.values()
    assert hashes["Grace"] is None and hashes["Tobi"] is None and hashes["Ola"] and hashes["Ada"]

    # The link in Ada's message is hers: her phone calling it is recorded as Ada arriving.
    await client.post(f"/presence/{links['Ada'][0]}", json={"event": "enter", "place": "Tesco Extra"})
    assert await rows("select m.name, p.name as place, p.kind from presence_events e join members m "
                      "on m.id = e.member_id join places p on p.id = e.place_id") == [
        {"name": "Ada", "place": "Tesco Extra", "kind": "store"}]

    await tool(home, "undo_last")                           # reopening the step takes nobody's link away
    assert (await fetch_state(home))["step"] == "rhythm"
    assert {r["name"]: r["presence_token_hash"] for r in await rows(
        "select name, presence_token_hash from members")} == hashes


async def fetch_state(home):
    async with tx() as conn:
        return await fetch_val(conn, "select onboarding_state from households where id = :h", h=home.id)


async def test_presence_can_be_skipped_or_asked_for_early_and_a_link_someone_has_is_never_replaced():
    async with tx() as conn:
        home = await setting_up(conn, reached="tour")
    await tool(home, "onboarding_advance", step="presence", skipped=True)
    await tool(home, "onboarding_advance", step="tour")
    result, _ = await tool(home, "onboarding_advance", step="rhythm", skipped=True)
    assert result == "OK: rhythm skipped. Setup is complete"
    assert await presence_messages() == {} and await rows(
        "select 1 from members where presence_token_hash is not null") == []

    async with tx() as conn:
        await execute(conn, "delete from households")
        home = await setting_up(conn, reached="tour")
        mine = await members.new_presence_token(conn, home.ola)      # Ola made a link on the Settings page already
    result, _ = await tool(home, "onboarding_advance", step="presence")      # "send me that shop link now"
    assert result.splitlines() == ["OK: presence done. Next step: tour",
                                   "NOTE: Ada got a private message with a personal link for shop-arrival "
                                   "nudges and the phone steps; setting it up is optional"]
    assert set(await presence_messages()) == {"Ada"}
    await tool(home, "onboarding_advance", step="tour")
    await tool(home, "onboarding_advance", step="rhythm")
    assert set(await presence_messages()) == {"Ada"}        # not sent twice
    async with tx() as conn:
        assert (await members.for_presence_token(conn, mine))["id"] == home.ola


async def test_a_household_left_at_the_presence_step_is_told_what_it_is_and_the_tool_finishes_it():
    async with tx() as conn:
        home = await setting_up(conn, reached="presence")
    llm = FakeLLM(call("onboarding_advance", step="presence"), say("All set. I've sent you each a link."))
    async with tx() as conn:
        await inbound.simulate_turn(conn, LoopRuntime(llm), home.id, home.ola, "anything else?")
    assert "Current step: presence. Remaining: presence." in llm.requests[0][0]
    assert STEP_GUIDE["presence"] in llm.requests[0][0]
    assert set(await presence_messages()) == {"Ola", "Ada"} and (await fetch_state(home))["step"] is None


async def test_an_adult_who_connects_after_setup_gets_the_welcome_and_then_their_link(client):
    async with tx() as conn:
        home = await setting_up(conn)
        grace_code = await members.create_invite(conn, home.members["Grace"], utcnow())
    await tool(home, "onboarding_advance", step="rhythm")
    await deliver()

    await post(client, tg_update(1, f"/start {grace_code}", user_id=1003, name="Grace"))
    (welcome, link) = await deliver()
    assert welcome == ("1003", f"Hi Grace, you're connected. {inbound.WELCOME}", None)
    assert link[0] == "1003" and URL.search(link[1])
    token = URL.search(link[1]).group(1)
    async with tx() as conn:
        assert (await members.for_presence_token(conn, token))["name"] == "Grace"

    # The same invite links her WhatsApp too. That must not replace the link her phone already uses.
    await post_whatsapp(client, wa_message(1, grace_code, user_id="GB.1000000000000000000103", name="Grace"))
    assert len(await presence_messages()) == 3
    async with tx() as conn:
        assert (await members.for_presence_token(conn, token))["name"] == "Grace"


@pytest.mark.parametrize("state", ['{"step": null, "done": []}',                               # set up before presence existed
                                   '{"step": null, "done": ["presence"], "skipped": ["presence"]}',   # said no thanks
                                   '{"step": "shops", "done": ["family", "routines"]}'])        # not there yet
async def test_no_link_is_sent_on_connecting_unless_setup_sent_them(client, state):
    async with tx() as conn:
        home = await seed_home(conn)
        await execute(conn, "update households set onboarding_state = cast(:state as jsonb)", state=state)
        ada = await add_member(conn, home, "Ada")
        code = await members.create_invite(conn, ada, utcnow())
    await post(client, tg_update(1, f"/start {code}", user_id=1002, name="Ada"))
    assert await deliver() == [("1002", f"Hi Ada, you're connected. {inbound.WELCOME}", None)]


# ---------------------------------------------------------------- remember: facts and settings
async def test_remember_saves_updates_and_forgets_facts_for_the_household_or_one_person():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_member(conn, home, "Ada")
    await tool(home, "remember", key="Milk brand", value="Cravendale")
    await tool(home, "remember", key="milk_brand", value="Arla")              # same fact, new value
    await tool(home, "remember", key="allergy", value="peanuts", about="Ada")
    await tool(home, "remember", key="allergy", value="shellfish", about="me")
    facts = "select f.key, f.value, m.name as member from household_facts f left join members m on m.id = f.member_id"
    assert sorted(await rows(facts), key=str) == sorted([
        {"key": "milk_brand", "value": "Arla", "member": None},
        {"key": "allergy", "value": "peanuts", "member": "Ada"},
        {"key": "allergy", "value": "shellfish", "member": "Ola"}], key=str)

    llm = FakeLLM(say("NOOP"))
    async with tx() as conn:
        await inbound.simulate_turn(conn, LoopRuntime(llm), home.id, home.ola, "hello")
    assert "Facts: milk_brand=Arla; Ada: allergy=peanuts; Ola: allergy=shellfish" in llm.requests[0][0]

    await tool(home, "remember", key="allergy", about="Ada")                  # no value: forget
    result, is_error = await tool(home, "remember", key="allergy", value="x", about="Nobody")
    assert is_error
    assert len(await rows(facts)) == 2
    await tool(home, "undo_last")                                             # brings Ada's allergy back
    assert {"key": "allergy", "value": "peanuts", "member": "Ada"} in await rows(facts)


async def test_staples_said_in_chat_go_on_the_list_by_themselves_when_they_run_out():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "rice", qty=2)
    result, _ = await tool(home, "remember", key="staples", value="rice, eggs,  Milk")
    assert "NEW: egg" in result and "NEW: Milk" in result
    assert {r["canonical_name"] for r in await rows("select canonical_name from items where is_staple")} == {
        "rice", "egg", "Milk"}
    assert await rows("select 1 from household_facts") == []                  # a setting, not a fact

    await tool(home, "log_inventory", changes=[{"item": "rice", "action": "finished"}])
    async with tx() as conn:
        assert await active_list(conn, home) == {"rice": "finished"}
    await tool(home, "undo_last", n=2)                                        # the finish, then the staples
    assert await rows("select canonical_name from items where is_staple") == []


async def test_shops_become_places_and_the_brief_time_and_quiet_hours_are_real_settings():
    async with tx() as conn:
        home = await seed_home(conn)
        ada = await add_member(conn, home, "Ada", telegram_id="1002")
        await add_member(conn, home, "Tobi", role="child")
    await tool(home, "remember", key="main_supermarket", value="Tesco Extra")
    await tool(home, "remember", key="shops", value="Tesco Extra, African shop on Rye Lane")
    assert await rows("select name, kind from places order by name") == [
        {"name": "African shop on Rye Lane", "kind": "store"}, {"name": "Tesco Extra", "kind": "store"}]
    assert {r["key"] for r in await rows("select key from household_facts")} == {"main_supermarket", "shops"}

    await tool(home, "remember", key="morning_brief", value="6:45am")
    assert await rows("select digest_time from households") == [{"digest_time": time(6, 45)}]

    quiet = "select name, quiet_start, quiet_end from members where role = 'adult' order by name"
    await tool(home, "remember", key="quiet_hours", value="22:00-06:30")      # nobody named: every adult
    assert await rows(quiet) == [{"name": "Ada", "quiet_start": time(22), "quiet_end": time(6, 30)},
                                 {"name": "Ola", "quiet_start": time(22), "quiet_end": time(6, 30)}]
    await tool(home, "remember", key="quiet_hours", value="11pm to 7am", about="me", speaker="Ada")
    await tool(home, "remember", key="quiet_hours", value="off", about="Ola")
    assert await rows(quiet) == [{"name": "Ada", "quiet_start": time(23), "quiet_end": time(7)},
                                 {"name": "Ola", "quiet_start": None, "quiet_end": None}]
    # The router acts on it: at 23:30 a reminder for Ada waits for 07:00, one for Ola goes out.
    async with tx() as conn:
        late = utcnow().replace(month=1, day=15, hour=23, minute=30)
        assert await members.quiet_until(conn, home.id, ada, late) == late.replace(day=16, hour=7, minute=0,
                                                                                    second=0, microsecond=0)
        assert await members.quiet_until(conn, home.id, home.ola, late) is None

    for bad in ({"key": "morning_brief", "value": "after breakfast"}, {"key": "quiet_hours", "value": "22:00"},
                {"key": "quiet_hours", "value": "22:00-25:00"}, {"key": "quiet_hours", "value": "21:00-07:00", "about": "Tobi"}):
        result, is_error = await tool(home, "remember", **bad)
        assert is_error, bad
    assert await rows("select digest_time from households") == [{"digest_time": time(6, 45)}]
    assert (await rows(quiet))[0]["quiet_start"] == time(23)


async def test_undoing_a_settings_change_never_revives_a_session_or_an_invite():
    async with tx() as conn:
        home = await seed_home(conn)
        ada = await add_member(conn, home, "Ada")
        await members.create_invite(conn, ada, utcnow())
        await link(conn, ada, "1002")
    await tool(home, "remember", key="quiet_hours", value="20:00-08:00")
    async with tx() as conn:                                # afterwards: Ola logs out everywhere, Ada's invite is revoked
        await members.log_out_everywhere(conn, home.ola)
        await execute(conn, "update members set invite_code_hash = null, invite_expires_at = null where id = :id",
                      id=ada)
    await tool(home, "undo_last")
    async with tx() as conn:
        ola = await fetch_one(conn, "select quiet_start, session_version from members where id = :id", id=home.ola)
        invite = await fetch_val(conn, "select invite_code_hash from members where id = :id", id=ada)
    assert ola == {"quiet_start": time(21, 30), "session_version": 2} and invite is None


@pytest.mark.parametrize("text,expected", [
    ("07:30", time(7, 30)), ("7.30", time(7, 30)), ("7am", time(7)), ("12am", time(0)), ("12pm", time(12)),
    ("9:30 PM", time(21, 30)), ("21:30:00", time(21, 30)), ("0:05", time(0, 5)),
])
def test_times_of_day_are_read_as_people_write_them(text, expected):
    assert parse_clock(text) == expected


@pytest.mark.parametrize("text", ["", "soon", "25:00", "7:75", "13pm", "0am", "7 o'clock"])
def test_things_that_are_not_times_are_refused(text):
    with pytest.raises(ValueError):
        parse_clock(text)
