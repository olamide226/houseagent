"""Undo: inverse generation, exact restore, conflict refusal, scope and replay (spec 9.3)."""
import random
from decimal import Decimal as D

from app.agent.actions import Recorder, record
from app.agent.tools import run_tool
from app.db import execute, fetch_all, fetch_one, tx
from app.services import inventory, shopping
from tests.helpers import (
    active_list,
    add_item,
    add_member,
    ctx_for,
    events_of,
    list_snapshot,
    location_id_of,
    seed_home,
    stock_snapshot,
)


async def log(ctx, item, action, **extra):
    result, is_error = await run_tool("log_inventory", {"changes": [{"item": item, "action": action, **extra}]}, ctx)
    assert not is_error, result


async def undo(ctx, n=1):
    return await run_tool("undo_last", {"n": n}, ctx)


async def test_inverse_restores_rows_that_existed_and_deletes_rows_that_did_not():
    async with tx() as conn:
        home = await seed_home(conn)
        egg = await add_item(conn, home, "egg", location="fridge", staple=True, qty=6)
        fridge = await location_id_of(conn, home, "fridge")
        rec = Recorder(ctx_for(conn, home))
        await inventory.apply_change(rec, inventory.Change(egg, "finished"), "message")

        restore, delete = rec.inverse()
        assert restore["op"] == "restore_rows" and restore["table"] == "stock"
        assert [(r["item_id"], r["location_id"], r["qty_estimate"], r["status"]) for r in restore["rows"]] == [
            (egg, fridge, 6, "in_stock")]
        entry = await fetch_one(conn, "select id from shopping_list_items")
        assert delete == {"op": "delete_rows", "table": "shopping_list_items", "ids": [entry["id"]]}
        assert {t["table"] for t in rec.touched} == {"stock", "inventory_events", "shopping_list_items"}


async def test_a_row_changed_twice_in_one_action_restores_to_its_first_state():
    async with tx() as conn:
        home = await seed_home(conn)
        rice = await add_item(conn, home, "rice", qty=5)
        rec = Recorder(ctx_for(conn, home))
        await inventory.apply_change(rec, inventory.Change(rice, "used", D(2)), "message")
        await inventory.apply_change(rec, inventory.Change(rice, "used", D(2)), "message")
        (restore,) = rec.inverse()
        assert [r["qty_estimate"] for r in restore["rows"]] == [5]


async def test_undo_restores_stock_and_list_exactly():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "rice", staple=True, qty=3)
        await add_item(conn, home, "milk", location="fridge", qty=2, threshold=1)
        ctx = ctx_for(conn, home)
        await run_tool("update_shopping_list", {"add": [{"item": "milk"}]}, ctx)
        stock_before, list_before = await stock_snapshot(conn, home), await list_snapshot(conn, home)

        await run_tool("log_inventory", {"changes": [
            {"item": "rice", "action": "finished"},                 # staple: adds a list row
            {"item": "milk", "action": "restocked", "quantity": 4}, # ticks the list row off
            {"item": "yam", "action": "added"},                     # new item, new stock row
        ]}, ctx)
        assert await stock_snapshot(conn, home) != stock_before

        result, is_error = await undo(ctx)
        assert not is_error and result.startswith("OK: undid log_inventory")
        assert await stock_snapshot(conn, home) == stock_before
        assert await list_snapshot(conn, home) == list_before


async def test_undo_appends_an_adjusted_event_and_deletes_no_history():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "rice", qty=3)
        ctx = ctx_for(conn, home)
        await log(ctx, "rice", "finished")
        await undo(ctx)
        assert await events_of(conn, home) == [("rice", "finished", None, "message"), ("rice", "adjusted", D(3), "undo")]


async def test_undo_reverts_an_auto_staple():
    async with tx() as conn:
        home = await seed_home(conn)
        ctx = ctx_for(conn, home)
        for action in ("restocked", "finished", "restocked", "finished"):
            await log(ctx, "plantain", action)
        assert (await fetch_one(conn, "select is_staple from items"))["is_staple"] is True
        await undo(ctx)
        assert (await fetch_one(conn, "select is_staple from items"))["is_staple"] is False
        assert await active_list(conn, home) == {}


async def test_undo_is_refused_when_a_later_action_touched_the_same_rows():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_member(conn, home, "Ada")
        await add_item(conn, home, "rice", qty=3)
        ola, ada = ctx_for(conn, home, "Ola"), ctx_for(conn, home, "Ada")
        await log(ola, "rice", "finished")
        await log(ada, "rice", "restocked", quantity=5)
        before = await stock_snapshot(conn, home)

        result, is_error = await undo(ola)
        assert is_error and "later log_inventory changed the same things" in result
        assert await stock_snapshot(conn, home) == before
        assert (await fetch_one(conn, "select count(*) as n from agent_actions where undone_at is not null"))["n"] == 0

        # Once the later action is undone, the earlier one can be.
        assert not (await undo(ada))[1]
        assert not (await undo(ola))[1]
        assert [(r["qty_estimate"], r["status"]) for r in await stock_snapshot(conn, home)] == [(D(3), "in_stock")]


async def test_unrelated_later_actions_do_not_block_undo():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_member(conn, home, "Ada")
        await add_item(conn, home, "rice", qty=3)
        await add_item(conn, home, "beans", qty=2)
        await log(ctx_for(conn, home, "Ola"), "rice", "finished")
        await log(ctx_for(conn, home, "Ada"), "beans", "finished")
        assert not (await undo(ctx_for(conn, home, "Ola")))[1]


async def test_undo_only_reverts_the_callers_own_actions_from_the_last_24_hours():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_member(conn, home, "Ada")
        await add_item(conn, home, "rice", qty=3)
        ola, ada = ctx_for(conn, home, "Ola"), ctx_for(conn, home, "Ada")
        await log(ola, "rice", "finished")

        result, is_error = await undo(ada)
        assert is_error and "nothing to undo" in result

        await execute(conn, "update agent_actions set created_at = created_at - interval '25 hours'")
        result, is_error = await undo(ola)
        assert is_error and "nothing to undo" in result


async def test_undo_n_reverts_the_newest_actions_first():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "rice", qty=10)
        ctx = ctx_for(conn, home)
        for used in (1, 2, 3):
            await log(ctx, "rice", "used", quantity=used)
        result, is_error = await undo(ctx, n=2)
        assert not is_error and len(result.splitlines()) == 2
        assert [r["qty_estimate"] for r in await stock_snapshot(conn, home)] == [D(9)]
        await undo(ctx)
        assert [r["qty_estimate"] for r in await stock_snapshot(conn, home)] == [D(10)]
        assert (await undo(ctx))[1]   # nothing left


async def test_dashboard_actions_are_logged_with_their_source_and_can_be_undone():
    async with tx() as conn:
        home = await seed_home(conn)
        egg = await add_item(conn, home, "egg", location="fridge")
        ctx = ctx_for(conn, home, source="dashboard")
        async with record(ctx, "shopping.add", {"item": "egg"}) as rec:
            await shopping.add(rec, egg, "egg")
        action = await fetch_one(conn, "select source, tool, result from agent_actions")
        assert (action["source"], action["tool"]) == ("dashboard", "shopping.add")
        await undo(ctx)
        assert await active_list(conn, home) == {}


async def test_stock_rebuilt_from_events_equals_live_stock_after_random_history():
    rng = random.Random(20261005)
    actions = ["added", "used", "low", "finished", "restocked", "adjusted", "discarded"]
    async with tx() as conn:
        home = await seed_home(conn)
        ctx = ctx_for(conn, home)
        for step in range(60):
            if step and rng.random() < 0.2:
                await undo(ctx, n=rng.randint(1, 2))
                continue
            change = {"item": rng.choice(["rice", "egg", "milk", "yam"]), "action": rng.choice(actions)}
            if rng.random() < 0.6:
                change["quantity"] = rng.randint(0, 6)
            if rng.random() < 0.3:
                change["location"] = rng.choice(["fridge", "freezer", "store"])
            await log(ctx, **change)

        live = await stock_snapshot(conn, home)
        assert live, "the history should have produced stock"
        await inventory.rebuild_stock(conn, home.id)
        assert await stock_snapshot(conn, home) == live
        assert len(await fetch_all(conn, "select 1 from inventory_events where source = 'undo'")) > 0
