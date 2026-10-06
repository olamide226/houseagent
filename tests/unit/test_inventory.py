"""Inventory and shopping tools: stock writes and the deterministic side-effect rules (spec 9.4)."""
from decimal import Decimal as D

from app.agent.tools import run_tool
from app.db import fetch_all, fetch_one, tx
from tests.helpers import active_list, add_item, ctx_for, events_of, seed_home, stock_of


def change(item, action, **extra):
    return {"item": item, "action": action, **extra}


async def log(ctx, *changes, source="message"):
    result, is_error = await run_tool("log_inventory", {"changes": list(changes), "source": source}, ctx)
    assert not is_error, result
    return result


async def test_batch_finish_adds_only_the_staple_to_the_list():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "egg", location="fridge", staple=True, qty=6)
        await add_item(conn, home, "bread", qty=1)
        result = await log(ctx_for(conn, home), change("eggs", "finished"), change("bread", "finished"))

        assert await stock_of(conn, home) == {("egg", "fridge"): (D(0), "out"), ("bread", "store"): (D(0), "out")}
        assert await active_list(conn, home) == {"egg": "finished"}
        assert [(e[0], e[1]) for e in await events_of(conn, home)] == [("egg", "finished"), ("bread", "finished")]
        # Both outcomes are spelled out: a model told nothing about bread adds it itself, or says it was added.
        assert result.splitlines() == [
            "OK: egg finished (fridge)", "NOTE: egg added to shopping list",
            "OK: bread finished (store)", "NOTE: bread not added to shopping list: it is not a staple"]


async def test_running_out_of_something_already_on_the_list_says_so_and_changes_nothing_there():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "egg", staple=True, qty=6)
        await add_item(conn, home, "bread", qty=1)
        ctx = ctx_for(conn, home)
        await run_tool("update_shopping_list", {"add": [{"item": "eggs"}, {"item": "bread"}]}, ctx)
        result = await log(ctx, change("eggs", "finished"), change("bread", "finished"))
        assert [line for line in result.splitlines() if line.startswith("NOTE:")] == [
            "NOTE: egg is already on the shopping list", "NOTE: bread is already on the shopping list"]
        assert await active_list(conn, home) == {"egg": "explicit", "bread": "explicit"}
        # An entry that was ticked off is no longer "on the list".
        await run_tool("update_shopping_list", {"bought": ["bread"]}, ctx)
        assert "NOTE: bread not added to shopping list: it is not a staple" in await log(ctx, change("bread", "finished"))


async def test_low_adds_any_item_to_the_list_once():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "rice", qty=2)
        ctx = ctx_for(conn, home)
        assert "NOTE: rice added to shopping list" in await log(ctx, change("rice", "low"))
        assert "NOTE: rice is already on the shopping list" in await log(ctx, change("rice", "low"))
        assert await stock_of(conn, home) == {("rice", "store"): (D(2), "low")}
        assert await active_list(conn, home) == {"rice": "low"}


async def test_restock_ticks_the_item_off_the_list():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "milk", location="fridge", staple=True, qty=1)
        ctx = ctx_for(conn, home)
        await log(ctx, change("milk", "finished"))
        result = await log(ctx, change("milk", "restocked", quantity=2, unit="pints"))
        assert await active_list(conn, home) == {}
        entry = await fetch_one(conn, "select status, resolved_at from shopping_list_items")
        assert entry["status"] == "bought" and entry["resolved_at"] is not None
        assert await stock_of(conn, home) == {("milk", "fridge"): (D(2), "in_stock")}
        assert "ticked off" in result


async def test_item_becomes_a_staple_after_two_restock_to_finished_cycles():
    async with tx() as conn:
        home = await seed_home(conn)
        ctx = ctx_for(conn, home)
        for cycle in (1, 2):
            await log(ctx, change("plantain", "restocked"))
            await log(ctx, change("plantain", "finished"))
            item = await fetch_one(conn, "select is_staple from items where canonical_name = 'plantain'")
            assert item["is_staple"] is (cycle == 2)
        assert await active_list(conn, home) == {"plantain": "finished"}


async def test_new_item_is_reported_and_goes_to_the_stated_location():
    async with tx() as conn:
        home = await seed_home(conn)
        result = await log(ctx_for(conn, home), change("Scotch bonnets", "added", location="deep freezer"))
        assert "NEW: Scotch bonnet" in result
        assert await stock_of(conn, home) == {("Scotch bonnet", "freezer"): (None, "in_stock")}


async def test_a_name_ending_in_one_the_household_has_is_not_recorded_until_the_model_says_which_it_is():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "milk", location="fridge", staple=True, qty=0, status="out")
        ctx = ctx_for(conn, home)
        await run_tool("update_shopping_list", {"add": [{"item": "milk"}]}, ctx)
        result = await log(ctx, change("semi skimmed milk", "restocked", quantity=2), change("coconut milk", "restocked"),
                           change("bleach", "restocked"), source="receipt")
        ask = ("ERROR: '{}' not recorded. The household already has milk (fridge). Decide which this is without "
               "asking: the same thing, then log it again under that name; a different product, then log it "
               "again with new_item true")
        assert result.splitlines() == [ask.format("semi skimmed milk"), ask.format("coconut milk"),
                                       "NEW: bleach", "OK: bleach restocked (store)"]
        assert [e[0] for e in await events_of(conn, home)] == ["bleach"]
        assert await active_list(conn, home) == {"milk": "explicit"}

        # The model answers: one is the household's milk, the other is something else.
        result = await log(ctx, change("milk", "restocked", quantity=2),
                           change("coconut milk", "restocked", new_item=True), source="receipt")
        assert "NEW: coconut milk" in result and "ERROR" not in result
        assert await stock_of(conn, home) == {("bleach", "store"): (None, "in_stock"), ("milk", "fridge"): (D(2), "in_stock"),
                                              ("coconut milk", "store"): (None, "in_stock")}
        assert await active_list(conn, home) == {}
        assert "ERROR" not in await log(ctx, change("coconut milk", "finished"))     # known from now on


async def test_ambiguous_name_is_skipped_while_other_changes_apply():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "Bell pepper", location="fridge", qty=2)
        await add_item(conn, home, "Black pepper", qty=1)
        await add_item(conn, home, "rice", qty=3)
        result = await log(ctx_for(conn, home), change("pepper", "finished"), change("rice", "used", quantity=1))
        assert "AMBIGUOUS: 'pepper' could be Bell pepper (fridge), Black pepper (store)" in result
        assert await stock_of(conn, home) == {
            ("Bell pepper", "fridge"): (D(2), "in_stock"),
            ("Black pepper", "store"): (D(1), "in_stock"),
            ("rice", "store"): (D(2), "in_stock"),
        }


async def test_change_without_a_location_goes_where_the_item_actually_is():
    async with tx() as conn:
        home = await seed_home(conn)
        ctx = ctx_for(conn, home)
        await add_item(conn, home, "butter")                       # default location: store
        await log(ctx, change("butter", "adjusted", quantity=2, location="fridge"), source="photo")
        await log(ctx, change("butter", "used", quantity=1))
        assert await stock_of(conn, home) == {("butter", "fridge"): (D(1), "in_stock")}


async def test_bought_with_no_quantity_logs_a_restock_at_the_default_location():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "egg", location="fridge")
        ctx = ctx_for(conn, home)
        await run_tool("update_shopping_list", {"add": [{"item": "eggs"}, {"item": "bin bags", "store_hint": "Costco"}]}, ctx)
        assert await active_list(conn, home) == {"egg": "explicit", "bin bag": "explicit"}

        result, is_error = await run_tool("update_shopping_list", {"bought": ["eggs"]}, ctx)
        assert not is_error
        assert await events_of(conn, home) == [("egg", "restocked", None, "shopping")]
        assert await stock_of(conn, home) == {("egg", "fridge"): (None, "in_stock")}
        assert await active_list(conn, home) == {"bin bag": "explicit"}


async def test_bought_all_restocks_everything_and_remove_dismisses():
    async with tx() as conn:
        home = await seed_home(conn)
        ctx = ctx_for(conn, home)
        await run_tool("update_shopping_list", {"add": [{"item": "eggs"}, {"item": "bread"}, {"item": "bleach"}]}, ctx)
        await run_tool("update_shopping_list", {"remove": ["bleach"]}, ctx)
        await run_tool("update_shopping_list", {"bought_all": True}, ctx)
        assert await active_list(conn, home) == {}
        statuses = {r["item"]: r["status"] for r in await fetch_all(
            conn, "select i.canonical_name as item, s.status from shopping_list_items s join items i on i.id = s.item_id")}
        assert statuses == {"egg": "bought", "bread": "bought", "bleach": "dismissed"}
        assert sorted(e[0] for e in await events_of(conn, home)) == ["bread", "egg"]


async def test_adding_an_item_twice_keeps_one_active_entry():
    async with tx() as conn:
        home = await seed_home(conn)
        ctx = ctx_for(conn, home)
        await run_tool("update_shopping_list", {"add": [{"item": "eggs"}]}, ctx)
        await run_tool("update_shopping_list", {"add": [{"item": "a dozen eggs"}]}, ctx)
        assert len(await fetch_all(conn, "select 1 from shopping_list_items")) == 1


async def test_get_shopping_list_and_query_inventory_read_back_state():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "egg", location="fridge", qty=6)
        await add_item(conn, home, "rice", status="low")
        ctx = ctx_for(conn, home)
        await run_tool("update_shopping_list", {"add": [{"item": "yam", "store_hint": "African shop"}, {"item": "bread"}]}, ctx)

        everything, _ = await run_tool("get_shopping_list", {}, ctx)
        assert "yam" in everything and "bread" in everything
        african, _ = await run_tool("get_shopping_list", {"store": "African shop"}, ctx)
        assert "yam" in african

        low, _ = await run_tool("query_inventory", {"status": ["low", "out"]}, ctx)
        assert "rice" in low and "egg" not in low
        fridge, _ = await run_tool("query_inventory", {"location": "fridge"}, ctx)
        assert "egg" in fridge and "6" in fridge and "rice" not in fridge
        unknown, _ = await run_tool("query_inventory", {"item": "caviar"}, ctx)
        assert "caviar" in unknown
        assert await fetch_one(conn, "select 1 from items where canonical_name = 'caviar'") is None


async def test_invalid_arguments_and_unknown_tools_are_tool_errors_that_write_nothing():
    async with tx() as conn:
        home = await seed_home(conn)
        ctx = ctx_for(conn, home)
        for name, args in [("log_inventory", {"changes": []}),
                           ("log_inventory", {"changes": [{"item": "egg", "action": "eaten"}]}),
                           ("send_money", {})]:
            result, is_error = await run_tool(name, args, ctx)
            assert is_error and result.startswith("ERROR:")
        assert await fetch_all(conn, "select 1 from agent_actions") == []


async def test_a_failed_tool_call_keeps_earlier_writes_in_the_turn():
    async with tx() as conn:
        home = await seed_home(conn)
        ctx = ctx_for(conn, home)
        await log(ctx, change("rice", "restocked", quantity=2))
        result, is_error = await run_tool("update_shopping_list", {"bought_all": True}, ctx)   # empty list
        assert is_error and "empty" in result
        assert await stock_of(conn, home) == {("rice", "store"): (None, "in_stock")}


async def test_logging_a_purchase_then_ticking_it_off_in_the_same_turn_is_one_restock():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "milk", location="fridge", staple=True, qty=0, status="out")
        message = await fetch_one(
            conn,
            """insert into messages (household_id, member_id, direction, text)
               values (:h, :m, 'in', 'I bought 2 pints of milk') returning id""", h=home.id, m=home.ola)
        ctx = ctx_for(conn, home, message_id=message["id"])
        await log(ctx, change("milk", "restocked", quantity=2, unit="pints"))
        result, is_error = await run_tool("update_shopping_list", {"bought": ["milk"]}, ctx)

        assert not is_error and "already recorded" in result
        assert await events_of(conn, home) == [("milk", "restocked", D(2), "message")]
        assert await stock_of(conn, home) == {("milk", "fridge"): (D(2), "in_stock")}

        # A later turn that ticks milk off is a new purchase.
        later = ctx_for(conn, home)
        await run_tool("update_shopping_list", {"bought": ["milk"]}, later)
        assert len(await events_of(conn, home)) == 2
