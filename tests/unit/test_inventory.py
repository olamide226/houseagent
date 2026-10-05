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
        assert "NOTE: egg added to shopping list" in result


async def test_low_adds_any_item_to_the_list_once():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "rice", qty=2)
        ctx = ctx_for(conn, home)
        await log(ctx, change("rice", "low"))
        await log(ctx, change("rice", "low"))
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
