"""LoopRuntime (spec section 8.2): the tool loop, final-answer policy, guards, history and brief."""
from datetime import UTC, datetime, timedelta

import pytest

from app.agent.loop import LOST, LoopRuntime
from app.agent.prompt import build_brief
from app.agent.tools import run_tool
from app.core.envelope import Channel, Envelope
from app.db import execute, fetch_val, tx
from app.llm.types import LLMError, LLMResponse, ToolCall, Usage
from tests.helpers import FakeLLM, active_list, add_item, add_member, call, ctx_for, say, seed_home, stock_of


def envelope(home, text, *, thread_id=None, message_ids=None, **extra):
    return Envelope(household_id=home.id, member_id=home.ola, member_name="Ola", thread_id=thread_id,
                    message_ids=message_ids or [], channel=Channel.telegram, scope="dm", text=text,
                    received_at=datetime.now(UTC), **extra)


async def test_tool_call_then_ack_records_and_reports_ack_only():
    llm = FakeLLM(call("log_inventory", changes=[{"item": "eggs", "action": "finished"}]), say("ACK"))
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "egg", location="fridge", staple=True, qty=6)
        result = await LoopRuntime(llm).handle(envelope(home, "we're out of eggs"), ctx_for(conn, home))

        assert (result.ack_only, result.noop, result.reply) == (True, False, None)
        assert [(c.name, c.is_error) for c in result.tool_calls] == [("log_inventory", False)]
        assert await active_list(conn, home) == {"egg": "finished"}
        assert (result.usage.input_tokens, result.usage.output_tokens) == (200, 25)   # summed over both calls
    # The second request carries the tool result back to the model.
    _, messages, tools = llm.requests[1]
    assert [m.role for m in messages] == ["user", "assistant", "tool"]
    assert messages[2].tool_call_id == "call_log_inventory" and "OK: egg finished" in messages[2].content[0].text
    assert {t.name for t in tools} == {
        "log_inventory", "query_inventory", "update_shopping_list", "get_shopping_list", "undo_last",
        "schedule_event", "modify_event", "list_upcoming", "set_reminder", "remember", "add_family_member"}


@pytest.mark.parametrize("answer,expected", [
    ("NOOP", (None, False, True)),
    ("  ACK\n", (None, True, False)),
    ("We have 6 eggs.", ("We have 6 eggs.", False, False)),
    ("", (None, False, True)),          # silence with nothing recorded is a no-op
])
async def test_final_answer_policy(answer, expected):
    async with tx() as conn:
        home = await seed_home(conn)
        result = await LoopRuntime(FakeLLM(say(answer))).handle(envelope(home, "hello"), ctx_for(conn, home))
        assert (result.reply, result.ack_only, result.noop) == expected


async def test_tool_errors_go_back_to_the_model_so_it_can_recover():
    bad_json = LLMResponse(text=None, stop="tool_calls", tool_calls=[
        ToolCall(id="c1", name="log_inventory", arguments={}, error="invalid JSON arguments")])
    llm = FakeLLM(bad_json, call("log_inventory", changes=[{"item": "rice", "action": "eaten"}]),
                  call("log_inventory", changes=[{"item": "rice", "action": "finished"}]), say("ACK"))
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "rice", qty=2)
        result = await LoopRuntime(llm).handle(envelope(home, "rice is done"), ctx_for(conn, home))
        assert [c.is_error for c in result.tool_calls] == [True, True, False]
        assert result.tool_calls[0].result == "ERROR: invalid JSON arguments"
        assert (await stock_of(conn, home))[("rice", "store")][1] == "out"
    tool_messages = [m for m in llm.requests[-1][1] if m.role == "tool"]
    assert [m.is_error for m in tool_messages] == [True, True, False]


async def test_iteration_cap_stops_a_runaway_loop_but_keeps_what_was_recorded():
    looping = [call("log_inventory", changes=[{"item": "rice", "action": "used", "quantity": 1}])] * 3
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "rice", qty=10)
        result = await LoopRuntime(FakeLLM(*looping), max_iterations=3).handle(
            envelope(home, "used rice"), ctx_for(conn, home))
        assert result.reply == LOST and len(result.tool_calls) == 3
        assert (await stock_of(conn, home))[("rice", "store")][0] == 7


async def test_a_model_that_stops_with_an_error_fails_the_turn():
    async with tx() as conn:
        home = await seed_home(conn)
        with pytest.raises(LLMError):
            await LoopRuntime(FakeLLM(LLMResponse(text=None, stop="error", usage=Usage()))).handle(
                envelope(home, "hi"), ctx_for(conn, home))


async def test_system_prompt_carries_the_agent_name_and_a_brief_of_current_state():
    llm = FakeLLM(say("NOOP"))
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "rice", status="low")
        await add_item(conn, home, "egg", location="fridge", staple=True, qty=1)
        ctx = ctx_for(conn, home)
        await run_tool("log_inventory", {"changes": [{"item": "egg", "action": "finished"}]}, ctx)
        await LoopRuntime(llm, agent_name="Hearth").handle(envelope(home, "hi"), ctx)
    system = llm.requests[0][0]
    assert system.startswith("You are Hearth, the household assistant")
    brief = system.split("<cache-break/>")[1]
    assert "(Europe/London)" in brief and "Speaking: Ola (telegram, dm)" in brief
    assert "Family: Ola (adult, set this up, on Telegram)" in brief and "Locations: freezer, fridge, store" in brief
    assert "Shopping list (1): egg" in brief
    assert "rice (low)" in brief and "egg (out)" in brief
    # What the speaker last recorded, which thread history does not show, so "undo" has something to mean.
    assert brief.endswith("Last change by Ola (what undo_last reverts): OK: egg finished (fridge); "
                          "NOTE: egg added to shopping list")


async def test_the_brief_names_only_the_speakers_own_last_change_that_can_still_be_undone():
    async with tx() as conn:
        home = await seed_home(conn)
        ada = await add_member(conn, home, "Ada")
        ola = ctx_for(conn, home)
        await run_tool("update_shopping_list", {"add": [{"item": "bleach"}]}, ola)
        await run_tool("get_shopping_list", {}, ola)                       # a read is not a change
        now = datetime.now(UTC)
        from_ada = envelope(home, "hi").model_copy(update={"member_id": ada, "member_name": "Ada"})
        assert "Last change" not in await build_brief(conn, from_ada, now)
        assert (await build_brief(conn, envelope(home, "undo that"), now)).endswith(
            "Last change by Ola (what undo_last reverts): OK: bleach added to shopping list")
        await run_tool("undo_last", {}, ola)
        assert "Last change" not in await build_brief(conn, envelope(home, "hi"), now)


async def test_history_is_the_threads_recent_messages_and_excludes_the_current_batch():
    llm = FakeLLM(say("NOOP"))
    async with tx() as conn:
        home = await seed_home(conn)
        thread = await fetch_val(conn, "insert into threads (household_id, channel, external_thread_id, scope) "
                                       "values (:h, 'telegram', '1001', 'dm') returning id", h=home.id)

        async def message(direction, text, age, meta="{}"):
            return await fetch_val(
                conn,
                """insert into messages (household_id, thread_id, member_id, direction, text, meta, external_id, created_at)
                   values (:h, :t, :m, :d, :text, cast(:meta as jsonb), gen_random_uuid()::text, now() - cast(:age as interval))
                   returning id""",
                h=home.id, t=thread, m=home.ola if direction == "in" else None, d=direction, text=text, age=age, meta=meta)

        await message("in", "three days ago", timedelta(days=3))
        await message("in", "out of eggs", timedelta(hours=2))
        await message("out", None, timedelta(minutes=119), '{"reaction": "\U0001F44D"}')
        await message("in", "do we have rice?", timedelta(hours=1))
        await message("out", "Yes, 2 bags.", timedelta(minutes=59))
        current = await message("in", "and bread?", timedelta(0))
        await LoopRuntime(llm).handle(
            envelope(home, "and bread?", thread_id=thread, message_ids=[current],
                     reply_to_text="Yes, 2 bags."), ctx_for(conn, home))
    sent = [(m.role, m.content[0].text) for m in llm.requests[0][1]]
    assert sent == [
        ("user", "Ola: out of eggs"), ("assistant", "ACK"),
        ("user", "Ola: do we have rice?"), ("assistant", "Yes, 2 bags."),
        ("user", '[replying to: "Yes, 2 bags."]\nand bread?'),
    ]


async def test_history_is_capped_at_twenty_messages():
    llm = FakeLLM(say("NOOP"))
    async with tx() as conn:
        home = await seed_home(conn)
        thread = await fetch_val(conn, "insert into threads (household_id, channel, external_thread_id, scope) "
                                       "values (:h, 'telegram', '1001', 'dm') returning id", h=home.id)
        for n in range(30):
            await execute(
                conn,
                """insert into messages (household_id, thread_id, member_id, direction, text, external_id, created_at)
                   values (:h, :t, :m, 'in', :text, :external, now() - cast(:age as interval))""",
                h=home.id, t=thread, m=home.ola, text=f"message {n}", external=str(n),
                age=timedelta(minutes=40 - n))
        await LoopRuntime(llm).handle(envelope(home, "now", thread_id=thread), ctx_for(conn, home))
    history = llm.requests[0][1][:-1]
    assert len(history) == 20 and history[0].content[0].text == "Ola: message 10"
