"""Meal ideas: what the model is given to cook with, and that picking a meal is an ordinary,
undoable change to the shopping list."""
from datetime import date

from app.agent.loop import LoopRuntime
from app.agent.prompt import LIST_CAP, build_brief
from app.agent.tools import run_tool
from app.agent.tools.meals import HOW_TO_ANSWER
from app.db import execute, fetch_val, tx
from app.pipeline.inbound import simulate_turn
from app.services import households, inventory
from app.worker.jobs import CATEGORIES
from tests.helpers import (
    FakeLLM,
    active_list,
    add_item,
    add_member,
    call,
    ctx_for,
    events_of,
    london,
    say,
    seed_home,
    stock_snapshot,
)
from tests.unit.test_loop import envelope

NOW = london("2026-10-05 12:00")   # a Monday


async def food(conn, home, name, location="store", *, qty=None, status="in_stock", unit=None, expires=None,
               category=None):
    """Something in the house, as a receipt, a photo and the nightly sorting would have left it."""
    item_id = await add_item(conn, home, name, location=location, qty=qty, status=status)
    await execute(conn, "update items set default_unit = :unit, category = :category where id = :id",
                  unit=unit, category=category, id=item_id)
    await execute(conn, "update stock set expires_on = :expires where item_id = :id",
                  expires=expires and date.fromisoformat(expires), id=item_id)
    return item_id


async def ideas(conn, home, now=NOW):
    """What there is to cook with, less the last line, which is always how to answer."""
    result, is_error = await run_tool("meal_ideas", {}, ctx_for(conn, home, now=now))
    assert not is_error and result.endswith("\n" + HOW_TO_ANSWER), result
    return result.removesuffix("\n" + HOW_TO_ANSWER)


async def test_what_needs_using_comes_first_and_what_ran_out_or_is_not_food_is_left_out():
    async with tx() as conn:
        home = await seed_home(conn)
        await food(conn, home, "chicken thigh", "fridge", qty=500, unit="g", expires="2026-10-07")
        await food(conn, home, "spinach", "fridge", expires="2026-10-06")
        await food(conn, home, "yoghurt", "fridge", expires="2026-10-03")
        await food(conn, home, "cheddar", "fridge", expires="2026-10-30")   # weeks away: nothing to hurry
        await food(conn, home, "egg", "fridge", qty=6)
        await food(conn, home, "pea", "freezer")
        await food(conn, home, "rice", qty=2, unit="kg", status="low")
        await food(conn, home, "pasta")
        await food(conn, home, "bread", qty=0, status="out")
        await food(conn, home, "bleach", category="household")
        await food(conn, home, "nappy", category="baby")
        before = await stock_snapshot(conn, home)

        assert (await ideas(conn, home)).splitlines() == [
            "Use first: spinach (fridge, by Tue 6 Oct), chicken thigh 500 g (fridge, by Wed 7 Oct)",
            "Past its date: yoghurt (fridge, Sat 3 Oct)",
            "freezer: pea",
            "fridge: cheddar, egg 6",
            "store: pasta, rice 2 kg (low)",
        ]
        # It only reads: no stock change, no event, nothing for undo to take back.
        assert await stock_snapshot(conn, home) == before and await events_of(conn, home) == []
        assert await fetch_val(conn, "select count(*) from agent_actions where household_id = :h", h=home.id) == 0


def test_what_is_not_food_is_named_by_the_sections_the_nightly_sorting_uses():
    assert set(inventory.NOT_FOOD) < set(CATEGORIES)


async def test_the_day_is_the_households_own():
    async with tx() as conn:
        home = await seed_home(conn)
        await food(conn, home, "salmon", "fridge", expires="2026-10-05")
        assert await ideas(conn, home, london("2026-10-05 23:30")) == "Use first: salmon (fridge, by Mon 5 Oct)"
        # Half past midnight in London is still the 5th in UTC.
        assert await ideas(conn, home, london("2026-10-06 00:30")) == "Past its date: salmon (fridge, Mon 5 Oct)"


async def test_an_empty_house_says_so():
    async with tx() as conn:
        home = await seed_home(conn)
        await food(conn, home, "bread", qty=0, status="out")
        await food(conn, home, "bleach", category="household")
        assert await run_tool("meal_ideas", {}, ctx_for(conn, home, now=NOW)) == (
            "OK: no food is recorded in the house yet", False)


async def test_a_long_pantry_stays_short():
    async with tx() as conn:
        home = await seed_home(conn)
        for n in range(150):
            await food(conn, home, f"tin {n:03}", qty=1)
        await execute(conn, "update stock set last_event_at = now() - make_interval(days => 400) "
                            "where item_id in (select id from items where canonical_name < 'tin 030')")
        await food(conn, home, "mince", "fridge", expires="2026-10-06")
        await execute(conn, "update stock set last_event_at = now() - make_interval(days => 900) "
                            "where item_id = (select id from items where canonical_name = 'mince')")
        result = await ideas(conn, home)
        every_row, _ = await run_tool("query_inventory", {}, ctx_for(conn, home, now=NOW))

    first, store, more = result.splitlines()
    # What has a date is kept however long ago it was recorded; the oldest of the rest are counted.
    assert first == "Use first: mince (fridge, by Tue 6 Oct)"
    shown = store.removeprefix("store: ").split(", ")
    assert len(shown) == inventory.COOKING_CAP - 1 and "tin 030 1" in shown and "tin 029 1" not in shown
    assert more == "+31 more not shown"
    assert len(result) < len(every_row) / 2      # against one line for each row, which is what a stock question gets


async def test_everything_remembered_comes_with_it_past_what_the_brief_has_room_for():
    async with tx() as conn:
        home = await seed_home(conn)
        tobi = await add_member(conn, home, "Tobi", role="child")
        await food(conn, home, "rice")
        for n in range(LIST_CAP):
            await execute(conn, "insert into household_facts (household_id, key, value) values (:h, :key, 'x')",
                          h=home.id, key=f"a_{n:02}")
        await execute(conn, "insert into household_facts (household_id, member_id, key, value) "
                            "values (:h, :m, 'allergy', 'peanuts')", h=home.id, m=tobi)
        result = await ideas(conn, home)
        brief = await build_brief(conn, envelope(home, "what's for dinner?"), NOW)

    assert "Tobi: allergy=peanuts" not in brief and "+1 more" in brief
    remembered = result.splitlines()[-1]
    assert remembered.startswith("Remembered: a_00=x; ") and remembered.endswith("; Tobi: allergy=peanuts")
    assert len(remembered.split("; ")) == len(await _facts(home)) == LIST_CAP + 1


async def _facts(home):
    async with tx() as conn:
        return await households.facts(conn, home.id)


async def test_picking_a_meal_puts_what_it_needs_on_the_list_and_undo_takes_it_off():
    suggestion = ("1. Chicken and spinach curry: you'd need coconut milk and curry paste\n"
                  "2. Spinach omelette: you have everything\nPick one and I'll put what's missing on the list.")
    llm = FakeLLM(call("meal_ideas"), say(suggestion),
                  call("update_shopping_list", add=[{"item": "coconut milk"}, {"item": "curry paste"}]),
                  say("Coconut milk and curry paste are on the list."),
                  call("undo_last"), say("Taken off again."))
    runtime = LoopRuntime(llm)
    async with tx() as conn:
        home = await seed_home(conn)
        await food(conn, home, "chicken thigh", "fridge", expires="2026-10-07")
        await food(conn, home, "spinach", "fridge", expires="2026-10-06")
        await food(conn, home, "egg", "fridge", qty=6)

        asked = await simulate_turn(conn, runtime, home.id, home.ola, "what can we cook tonight?", now=NOW)
        assert asked.reply == suggestion and await active_list(conn, home) == {}
        assert asked.tool_calls[0].result.splitlines() == [
            "Use first: spinach (fridge, by Tue 6 Oct), chicken thigh (fridge, by Wed 7 Oct)", "fridge: egg 6",
            HOW_TO_ANSWER]

        picked = await simulate_turn(conn, runtime, home.id, home.ola, "the first one", now=london("2026-10-05 12:01"))
        assert picked.reply == "Coconut milk and curry paste are on the list."
        assert await active_list(conn, home) == {"coconut milk": "explicit", "curry paste": "explicit"}
        # The pick is answered from the thread: the suggestion is there for the model to read.
        history = [m.content[0].text for m in llm.requests[2][1]]
        assert history == ["Ola: what can we cook tonight?", suggestion, "the first one"]

        await simulate_turn(conn, runtime, home.id, home.ola, "undo", now=london("2026-10-05 12:02"))
        assert await active_list(conn, home) == {}
        assert await events_of(conn, home) == []     # a meal is not a purchase: stock is as it was
