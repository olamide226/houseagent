"""Spending from receipts (ADR 0040): what a receipt that is read keeps, what is added up from it,
and that nothing a person is told was made up: no price that was not printed, and no zero for a
receipt whose prices were never kept."""
import re
from datetime import date
from decimal import Decimal as D

import pytest

from app.agent.tools import run_tool
from app.db import execute, fetch_all, fetch_one, fetch_val, tx
from app.pipeline.inbound import PLAYGROUND, _upsert_thread
from app.services import inventory, spending
from app.worker import jobs
from tests.helpers import (
    active_list,
    add_item,
    add_member,
    add_receipt,
    ctx_for,
    events_of,
    london,
    seed_home,
    sort_items,
    stock_of,
    technical_words,
)
from tests.unit.test_dashboard import login

NOW = london("2026-10-10 12:00")   # a Saturday
SUPERMARKET = {
    "changes": [
        {"item": "milk", "action": "restocked", "quantity": 2, "unit": "pints", "price": 1.65},
        {"item": "eggs", "action": "restocked", "quantity": 12, "price": 2.95},
        {"item": "bread", "action": "restocked", "price": 1.20}],
    "source": "receipt",
    "receipt": {"shop": "FRESHWAY SUPERMARKET", "bought_on": "2026-10-06", "total": 5.80},
}


async def a_message(conn, home, member="Ola"):
    """The photo a receipt came in on, and a context for the turn that reads it."""
    thread = await _upsert_thread(conn, home.id, PLAYGROUND, f"{PLAYGROUND}:{home.members[member]}", "dm")
    message = await fetch_val(
        conn, "insert into messages (household_id, thread_id, member_id, direction, text) "
              "values (:h, :thread, :member, 'in', '[photo]') returning id", h=home.id, thread=thread,
        member=home.members[member])
    return ctx_for(conn, home, member, message_id=str(message), now=NOW)


async def tool(ctx, name, **args):
    result, is_error = await run_tool(name, args, ctx)
    assert not is_error, result
    return result


async def receipts(conn, home, status="kept"):
    return await fetch_all(
        conn, "select shop, purchased_on, total, currency, status from receipts where household_id = :h "
              "and status = :status order by created_at", h=home.id, status=status)


async def lines(conn, home):
    rows = await fetch_all(
        conn, """select l.label, l.price, l.quantity, l.unit from receipt_lines l join receipts r on r.id = l.receipt_id
                 where r.household_id = :h order by r.created_at, l.position""", h=home.id)
    return [(row["label"], row["price"], row["quantity"], row["unit"]) for row in rows]


async def a_month_of_receipts(conn, home):
    """September: one shop. October: two at Freshway and one at Hilltop, 37.44 in all."""
    await add_receipt(conn, home, "Freshway Supermarket", "2026-09-20", 5.60,
                      (("milk", 1.55, 2, "pints"), ("egg", 2.95, 12), ("bread", 1.10)), staples=True)
    await add_receipt(conn, home, "Freshway Supermarket", "2026-10-03", 22.69, (
        ("rice", 9.50, 5, "kg"), ("chicken thighs", 4.75), ("butter", 1.99), ("tomato", 0.95), ("onion", 1.10),
        ("bleach", 0.85), ("toilet roll", 4.25)))                  # a bag at 0.30 and 1.00 off are not lines
    await add_receipt(conn, home, "Freshway Supermarket", "2026-10-06", 5.80,
                      (("milk", 1.65, 2, "pints"), ("egg", 2.95, 12), ("bread", 1.20)))
    await add_receipt(conn, home, "Hilltop Grocers", "2026-10-08", 8.95, (("butter", 2.40), ("coconut milk", 1.10)))
    await sort_items(conn, home, rice="cupboard", chicken_thighs="meat and fish", butter="dairy and eggs",
                     tomato="fruit and veg", onion="fruit and veg", bleach="household", toilet_roll="household",
                     milk="dairy and eggs", egg="dairy and eggs", bread="bakery")


# ---------------------------------------------------------------- keeping a receipt
async def test_a_receipt_keeps_its_shop_date_total_and_line_prices_and_stock_is_logged_as_before():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "milk", location="fridge", staple=True, status="out")
        await add_item(conn, home, "egg", location="fridge", qty=2)
        await tool(ctx_for(conn, home), "update_shopping_list", add=[{"item": "milk"}])
        result = await tool(await a_message(conn, home), "log_inventory", **SUPERMARKET)

        # Stock, the event log and the list are what the same receipt gave before prices were kept.
        assert await stock_of(conn, home) == {("milk", "fridge"): (None, "in_stock"), ("egg", "fridge"): (D(14), "in_stock"),
                                              ("bread", "store"): (None, "in_stock")}
        assert [event for event in await events_of(conn, home) if event[1] == "restocked"] == [
            ("milk", "restocked", D(2), "receipt"), ("egg", "restocked", D(12), "receipt"),
            ("bread", "restocked", None, "receipt")]
        assert await active_list(conn, home) == {}
        assert result.splitlines() == [
            "OK: milk restocked, 2 pints (fridge)", "NOTE: milk ticked off the shopping list",
            "OK: egg restocked, 12 (fridge)", "NEW: bread", "OK: bread restocked (store)",
            "OK: receipt kept for the spending: Freshway Supermarket, Tue 6 Oct, £5.80, 3 line prices"]

        assert await receipts(conn, home) == [{"shop": "Freshway Supermarket", "purchased_on": date(2026, 10, 6),
                                               "total": D("5.80"), "currency": "GBP", "status": "kept"}]
        assert await lines(conn, home) == [("milk", D("1.65"), D(2), "pints"), ("egg", D("2.95"), D(12), None),
                                           ("bread", D("1.20"), None, None)]


async def test_only_what_is_printed_is_kept_a_missing_price_or_total_is_never_filled_in():
    async with tx() as conn:
        home = await seed_home(conn)
        # One line has no price, and the total is torn off: one line is kept, and no total.
        torn = await tool(await a_message(conn, home), "log_inventory", source="receipt", receipt={"shop": "Rye Lane Market"},
                          changes=[{"item": "yam", "action": "restocked"},
                                   {"item": "plantain", "action": "restocked", "quantity": 4, "price": 2.00}])
        assert await lines(conn, home) == [("plantain", D("2.00"), D(4), None)]
        assert await receipts(conn, home) == [{"shop": "Rye Lane Market", "purchased_on": date(2026, 10, 10),
                                               "total": None, "currency": "GBP", "status": "kept"}]
        assert torn.splitlines()[-2:] == [
            "OK: receipt kept for the spending: Rye Lane Market, Sat 10 Oct, £2.00 from 1 line price; no total could "
            "be read on it", "NOTE: no date on the receipt could be used, so it counts as bought today"]

        # Nothing with a price on it: the stock is logged and there is no receipt to add up.
        nothing = await tool(await a_message(conn, home), "log_inventory", source="receipt",
                             changes=[{"item": "bleach", "action": "restocked"}])
        assert nothing.splitlines() == ["NEW: bleach", "OK: bleach restocked (store)",
                                        "NOTE: no price could be read on this receipt, so it is not in the spending"]
        # A price that was only said is not a receipt.
        said = await tool(await a_message(conn, home), "log_inventory",
                          changes=[{"item": "bread", "action": "restocked", "price": 1.20}])
        assert said.splitlines()[-1] == "NOTE: prices are kept from receipts only, so nothing was added to the spending"
        assert len(await receipts(conn, home)) == 1 and len(await lines(conn, home)) == 1
        assert [event[0] for event in await events_of(conn, home)] == ["yam", "plantain", "bleach", "bread"]

        result, is_error = await run_tool("log_inventory", {"source": "receipt", "changes": [
            {"item": "rice", "action": "restocked", "price": -1}]}, await a_message(conn, home))
        assert is_error and "price" in result


async def test_undo_takes_the_receipt_back_with_the_stock_it_logged():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "milk", location="fridge", staple=True, status="out")
        ctx = await a_message(conn, home)
        await tool(ctx, "log_inventory", **SUPERMARKET)
        assert "OK: undid log_inventory" in await tool(ctx, "undo_last")
        assert await receipts(conn, home) == [] and await lines(conn, home) == []
        assert (await stock_of(conn, home))[("milk", "fridge")] == (None, "out")
        found = await spending.summary(conn, home.id, date(2026, 10, 1), date(2026, 10, 31))
        assert (found.total, found.receipts, found.unpriced) == (D(0), 0, 0)   # an undone receipt was never read


async def test_one_receipt_logged_in_two_calls_is_one_receipt_and_nothing_is_kept_twice():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "rice", staple=True, status="out")
        ctx = await a_message(conn, home)
        header = {"shop": "FRESHWAY SUPERMARKET", "bought_on": "2026-10-03", "total": 22.69}
        first = await tool(ctx, "log_inventory", source="receipt", receipt=header, changes=[
            {"item": "basmati rice", "action": "restocked", "quantity": 5, "unit": "kg", "price": 9.50},
            {"item": "butter", "action": "restocked", "price": 1.99}])
        assert "ERROR: 'basmati rice' not recorded" in first
        assert first.splitlines()[-1] == ("OK: receipt kept for the spending: Freshway Supermarket, Sat 3 Oct, "
                                          "£22.69, 1 line price")
        # The model logs the rice under the household's name, and repeats the butter it already logged.
        second = await tool(ctx, "log_inventory", source="receipt", changes=[
            {"item": "rice", "action": "restocked", "quantity": 5, "unit": "kg", "price": 9.50},
            {"item": "butter", "action": "restocked", "price": 1.99}])
        assert second.splitlines()[-1] == ("OK: 1 more line price kept on the receipt (Freshway Supermarket, "
                                           "Sat 3 Oct, £22.69)")
        assert len(await receipts(conn, home)) == 1
        assert await lines(conn, home) == [("butter", D("1.99"), None, None), ("rice", D("9.50"), D(5), "kg")]

        # Undo reverts the second call; the first cannot go while the second stands.
        await tool(ctx, "undo_last")
        assert await lines(conn, home) == [("butter", D("1.99"), None, None)]

        # A second call that leaves the source out is still the rest of this turn's receipt, for the
        # stock event as for the price. Without a price it is taken as it was sent.
        again = await tool(ctx, "log_inventory", changes=[
            {"item": "rice", "action": "restocked", "quantity": 5, "unit": "kg", "price": 9.50}])
        assert again.splitlines()[-1].startswith("OK: 1 more line price kept on the receipt")
        await tool(ctx, "log_inventory", changes=[{"item": "bleach", "action": "finished"}])
        assert [(e[0], e[3]) for e in await events_of(conn, home) if e[3] != "undo"][-2:] == [
            ("rice", "receipt"), ("bleach", "message")]
        assert len(await receipts(conn, home)) == 1 and len(await lines(conn, home)) == 2
        # In another turn a price that is only said is still not spending.
        other = await tool(await a_message(conn, home), "log_inventory", changes=[
            {"item": "rice", "action": "restocked", "price": 9.50}])
        assert other.splitlines()[-1] == "NOTE: prices are kept from receipts only, so nothing was added to the spending"
        assert len(await lines(conn, home)) == 2

    async with tx() as conn:    # every priced line failed and there is no total yet: nothing is said too soon
        home = await seed_home(conn, telegram_id=None)
        await add_item(conn, home, "milk", staple=True, status="out")
        ctx = await a_message(conn, home)
        first = await tool(ctx, "log_inventory", source="receipt", receipt={"shop": "Hilltop Grocers"}, changes=[
            {"item": "coconut milk", "action": "restocked", "price": 1.10}])
        assert "no price could be read" not in first and await receipts(conn, home) == []
        await tool(ctx, "log_inventory", source="receipt", receipt={"shop": "Hilltop Grocers", "total": 1.10}, changes=[
            {"item": "coconut milk", "action": "restocked", "price": 1.10, "new_item": True}])
        assert [(r["shop"], r["total"]) for r in await receipts(conn, home)] == [("Hilltop Grocers", D("1.10"))]


async def test_the_same_receipt_sent_again_is_not_counted_twice():
    async with tx() as conn:
        home = await seed_home(conn)
        await tool(await a_message(conn, home), "log_inventory", **SUPERMARKET)
        again = await tool(await a_message(conn, home, ), "log_inventory", **SUPERMARKET)
        assert again.splitlines()[-1] == ("NOTE: this receipt was already kept (Freshway Supermarket, Tue 6 Oct, "
                                          "£5.80), so it is not counted twice in the spending")
        assert len(await receipts(conn, home)) == 1 and len(await lines(conn, home)) == 3
        found = await spending.summary(conn, home.id, date(2026, 10, 1), date(2026, 10, 31))
        # Counted once, and the second photo is not mistaken for a receipt read without prices.
        assert (found.total, found.receipts, found.unpriced) == (D("5.80"), 1, 0)
        # Another shop on the same day for the same money is another receipt.
        other = {**SUPERMARKET, "receipt": {**SUPERMARKET["receipt"], "shop": "Hilltop Grocers"}}
        await tool(await a_message(conn, home), "log_inventory", **other)
        assert len(await receipts(conn, home)) == 2


async def test_a_shop_is_filed_under_the_name_the_household_uses_and_a_date_after_today_counts_as_today():
    async with tx() as conn:
        home = await seed_home(conn)
        ctx = await a_message(conn, home)
        await tool(ctx, "remember", key="shops", value="Tesco, Rye Lane Market")
        for printed, kept in (("TESCO STORES 2041", "Tesco"), ("tesco", "Tesco"), ("ALDI", "Aldi"),
                              ("Aldi Stores Ltd", "Aldi"), ("Hilltop Grocers", "Hilltop Grocers"), ("  ", None)):
            await tool(await a_message(conn, home), "log_inventory", source="receipt",
                       receipt={"shop": printed, "bought_on": "2026-12-25", "total": 3},
                       changes=[{"item": "bread", "action": "restocked"}])
            newest = (await receipts(conn, home))[-1]
            assert (newest["shop"], newest["purchased_on"]) == (kept, date(2026, 10, 10)), printed


# ---------------------------------------------------------------- adding up
async def test_a_month_is_added_up_by_shop_and_by_kind_and_says_what_the_lines_do_not_account_for():
    async with tx() as conn:
        home = await seed_home(conn)
        await a_month_of_receipts(conn, home)
        october = await spending.summary(conn, home.id, date(2026, 10, 1), date(2026, 10, 31))
        assert (october.total, october.receipts, october.currency) == (D("37.44"), 3, "GBP")
        assert october.shops == [("Freshway Supermarket", D("28.49")), ("Hilltop Grocers", D("8.95"))]
        assert october.kinds == [
            ("cupboard", D("9.50")), ("dairy and eggs", D("8.99")), ("household", D("5.10")),
            ("meat and fish", D("4.75")), ("fruit and veg", D("2.05")), ("bakery", D("1.20")),
            (spending.UNSORTED, D("1.10"))]
        # Hilltop's total holds 5.45 of lines with no price kept; Freshway took 0.70 off at the till.
        assert (october.unlisted, october.discounts) == (D("5.45"), D("-0.70"))
        assert sum((spent for _, spent in october.kinds), D(0)) + october.unlisted + october.discounts == october.total

        hilltop = await spending.summary(conn, home.id, date(2026, 10, 1), date(2026, 10, 31), shop="hilltop")
        assert (hilltop.total, hilltop.receipts, hilltop.shops) == (D("8.95"), 1, [("Hilltop Grocers", D("8.95"))])
        week = await spending.summary(conn, home.id, date(2026, 10, 4), date(2026, 10, 10))
        assert (week.total, week.receipts) == (D("14.75"), 2)
        assert [(m.start, m.total, m.receipts) for m in await spending.months(conn, home.id, date(2026, 10, 10), 3)] == [
            (date(2026, 8, 1), D(0), 0), (date(2026, 9, 1), D("5.60"), 1), (date(2026, 10, 1), D("37.44"), 3)]

        # A receipt taken out, and one kept in another currency, are not added in.
        hilltop_id = await fetch_val(conn, "select id from receipts where shop = 'Hilltop Grocers'")
        await execute(conn, "update receipts set status = 'removed' where id = :id", id=hilltop_id)
        await execute(conn, "update receipts set currency = 'EUR' where purchased_on = '2026-10-06'")
        left = await spending.summary(conn, home.id, date(2026, 10, 1), date(2026, 10, 31))
        assert (left.total, left.receipts, left.elsewhere) == (D("22.69"), 1, 1)
        assert "NOTE: 1 receipt in another currency is not added in" in spending.summary_lines(left)


async def test_a_receipt_read_before_prices_were_kept_is_said_to_have_none_and_is_never_a_zero():
    async with tx() as conn:
        home = await seed_home(conn)
        # As every receipt was logged before this was built: stock events from a receipt, and no prices.
        for _ in range(2):
            await tool(await a_message(conn, home), "log_inventory", source="receipt",
                       changes=[{"item": "milk", "action": "restocked"}, {"item": "bread", "action": "restocked"}])
        await tool(await a_message(conn, home), "log_inventory", changes=[{"item": "rice", "action": "finished"}])
        await execute(conn, "update inventory_events set occurred_at = :at", at=NOW)
        assert await receipts(conn, home) == []
        found = await spending.summary(conn, home.id, date(2026, 10, 1), date(2026, 10, 31))
        assert (found.total, found.receipts, found.unpriced) == (D(0), 0, 2)

        asked = await tool(ctx_for(conn, home, now=NOW), "get_spending")
        assert asked.splitlines() == [
            "OK: no receipt with prices for 1 Oct to 10 Oct 2026",
            "NOTE: 2 receipts were read in this time without prices being kept, so what was spent is not known. "
            "Say that; do not say nothing was spent"]
        assert "£0" not in asked and "0.00" not in asked

        # With one priced receipt beside them, the total is that receipt and the gap is still said.
        await add_receipt(conn, home, "Freshway Supermarket", "2026-10-06", 5.80, (("milk", 1.65),))
        asked = await tool(ctx_for(conn, home, now=NOW), "get_spending")
        assert asked.splitlines()[0] == "OK: spent, 1 Oct to 10 Oct 2026: £5.80 on 1 receipt"
        assert asked.splitlines()[-1] == ("NOTE: 2 more receipts read in this time have no prices kept: what they "
                                          "cost is not known and is not in the total")
        # A month with nothing at all is only that.
        assert await tool(ctx_for(conn, home, now=NOW), "get_spending", start="2026-08-01", end="2026-08-31") == (
            "OK: no receipt with prices for August 2026")


async def test_what_was_spent_is_answered_for_a_time_a_shop_a_kind_of_thing_or_one_item():
    async with tx() as conn:
        home = await seed_home(conn)
        await a_month_of_receipts(conn, home)
        ctx = ctx_for(conn, home, now=NOW)
        assert (await tool(ctx, "get_spending")).splitlines() == [
            "OK: spent, 1 Oct to 10 Oct 2026: £37.44 on 3 receipts",
            "By shop: Freshway Supermarket £28.49; Hilltop Grocers £8.95",
            "By kind: cupboard £9.50; dairy and eggs £8.99; household £5.10; meat and fish £4.75; fruit and veg £2.05; "
            "bakery £1.20; not sorted yet £1.10; not listed line by line £5.45; discounts -£0.70",
            "Price change: bread at Freshway Supermarket: £1.20, was £1.10 (up £0.10)",
            "Price change: milk at Freshway Supermarket: £1.65, was £1.55 (up £0.10)"]
        assert (await tool(ctx, "get_spending", start="2026-09-01", end="2026-09-30")).splitlines()[0] == (
            "OK: spent, September 2026: £5.60 on 1 receipt")
        assert (await tool(ctx, "get_spending", shop="Hilltop")).splitlines()[:2] == [
            "OK: spent at Hilltop, 1 Oct to 10 Oct 2026: £8.95 on 1 receipt",
            "By kind: dairy and eggs £2.40; not sorted yet £1.10; not listed line by line £5.45"]

        assert (await tool(ctx, "get_spending", on="food")).splitlines() == [
            "OK: spent on food, 1 Oct to 10 Oct 2026: £26.49 on 3 receipts, from the price of each line",
            "NOTE: £1.10 more is on lines not sorted into a kind yet"]
        assert (await tool(ctx, "get_spending", on="Household")).splitlines() == [
            "OK: spent on household, 1 Oct to 10 Oct 2026: £5.10 on 1 receipt, from the price of each line",
            "NOTE: £1.10 more is on lines not sorted into a kind yet"]
        assert (await tool(ctx, "get_spending", on="meat")).splitlines()[0].startswith(
            "OK: spent on meat and fish, 1 Oct to 10 Oct 2026: £4.75 on 1 receipt")
        assert (await tool(ctx, "get_spending", on="milk", start="2026-09-01")).splitlines() == [
            "OK: spent on milk, 1 Sep to 10 Oct 2026: £3.20 on 2 receipts, from the price of each line",
            "Last paid: £1.65 at Freshway Supermarket on Tue 6 Oct; £1.55 at Freshway Supermarket on Sun 20 Sep"]
        assert (await tool(ctx, "get_spending", on="butter", shop="Hilltop")).splitlines()[0] == (
            "OK: spent on butter at Hilltop, 1 Oct to 10 Oct 2026: £2.40 on 1 receipt, from the price of each line")

        unknown, is_error = await run_tool("get_spending", {"on": "holidays"}, ctx)
        assert is_error and unknown.startswith("ERROR: nothing called 'holidays' has been bought on a receipt")
        backwards, is_error = await run_tool("get_spending", {"start": "2026-10-09", "end": "2026-10-01"}, ctx)
        assert is_error
        assert await fetch_all(conn, "select 1 from agent_actions where tool = 'get_spending'") == []   # a read


# ---------------------------------------------------------------- price changes
def bought(item, price, shop, day, quantity=None, unit=None, receipt=None):
    return {"item_id": item, "item": item, "price": D(str(price)), "quantity": None if quantity is None else D(quantity),
            "unit": unit, "shop": shop, "purchased_on": date.fromisoformat(day), "receipt_id": receipt or f"{shop}{day}"}


@pytest.mark.parametrize("earlier,newest,expected", [
    # The same pack: what it cost.
    (bought("milk", 1.55, "Freshway", "2026-09-20", 2, "pints"), bought("milk", 1.65, "Freshway", "2026-10-06", 2, "pint"),
     "milk at Freshway: £1.65, was £1.55 (up £0.10)"),
    (bought("bread", 1.20, "Freshway", "2026-09-20"), bought("bread", 1.05, "freshway", "2026-10-06"),
     "bread at freshway: £1.05, was £1.20 (down £0.15)"),
    # Another size in the same unit: what one of that unit cost.
    (bought("rice", 9.50, "Freshway", "2026-09-20", 5, "kg"), bought("rice", 3.15, "Freshway", "2026-10-06", 2, "kg"),
     "rice at Freshway: £1.58 a kg, was £1.90 a kg (down £0.32)"),
    (bought("egg", 1.50, "Freshway", "2026-09-20", 6), bought("egg", 3.30, "Freshway", "2026-10-06", 12),
     "egg at Freshway: £0.28 each, was £0.25 each (up £0.03)"),
    # No honest comparison: another shop, a size given once only, another unit, the same price, the same receipt.
    (bought("milk", 1.55, "Hilltop", "2026-09-20"), bought("milk", 1.65, "Freshway", "2026-10-06"), None),
    (bought("rice", 9.50, "Freshway", "2026-09-20", 5, "kg"), bought("rice", 3.15, "Freshway", "2026-10-06"), None),
    (bought("rice", 9.50, "Freshway", "2026-09-20", 5, "kg"), bought("rice", 3.15, "Freshway", "2026-10-06", 2, "bags"), None),
    (bought("milk", 1.65, "Freshway", "2026-09-20"), bought("milk", 1.65, "Freshway", "2026-10-06"), None),
    (bought("milk", 1.55, "Freshway", "2026-10-06", receipt="r"), bought("milk", 1.65, "Freshway", "2026-10-06", receipt="r"), None),
])
def test_a_price_change_is_the_newest_price_against_the_last_comparable_one_at_the_same_shop(earlier, newest, expected):
    changes = spending.price_changes([earlier, newest], date(2026, 10, 1), date(2026, 10, 31))
    assert [spending.change_text(change, "GBP") for change in changes] == ([expected] if expected else [])


def test_price_changes_look_past_purchases_that_cannot_be_compared_and_only_at_this_time():
    history = [bought("milk", 1.45, "Freshway", "2026-08-02"), bought("milk", 1.90, "Hilltop", "2026-09-01"),
               bought("milk", 1.65, "Freshway", "2026-10-06"), bought("bread", 1.00, "Freshway", "2026-08-02"),
               bought("bread", 1.10, "Freshway", "2026-09-20"), bought("milk", 1.70, "Freshway", "2026-11-02")]
    october = spending.price_changes(history, date(2026, 10, 1), date(2026, 10, 31))
    assert [(c.item, c.was, c.now) for c in october] == [("milk", D("1.45"), D("1.65"))]   # bread was not bought in October
    september = spending.price_changes(history, date(2026, 9, 1), date(2026, 9, 30))
    assert [(c.item, c.was, c.now) for c in september] == [("bread", D("1.00"), D("1.10"))]   # milk's first at Hilltop


async def test_price_changes_are_for_what_the_household_always_keeps_in():
    async with tx() as conn:
        home = await seed_home(conn)
        await a_month_of_receipts(conn, home)       # milk, egg and bread are staples; butter is not
        await add_receipt(conn, home, "Hilltop Grocers", "2026-10-09", 2.60, (("butter", 2.60),))
        changes = await spending.staple_price_changes(conn, home.id, date(2026, 10, 1), date(2026, 10, 31))
        assert [(c.item, c.was, c.now) for c in changes] == [("bread", D("1.10"), D("1.20")), ("milk", D("1.55"), D("1.65"))]
        await execute(conn, "update items set is_staple = true where canonical_name = 'butter'")
        changes = await spending.staple_price_changes(conn, home.id, date(2026, 10, 1), date(2026, 10, 31))
        assert ("butter", D("2.40"), D("2.60")) in [(c.item, c.was, c.now) for c in changes]


# ---------------------------------------------------------------- correcting and removing
async def test_a_receipt_is_corrected_or_taken_out_from_chat_and_undo_puts_it_back():
    async with tx() as conn:
        home = await seed_home(conn)
        await a_month_of_receipts(conn, home)
        ctx = ctx_for(conn, home, now=NOW)
        stock_before = await stock_of(conn, home)

        # With nothing said about which, it is the newest one.
        fixed = await tool(ctx, "update_receipt", new_total=9.85, prices=[{"item": "butter", "price": 2.45}])
        assert fixed == "OK: receipt corrected: Hilltop Grocers, Thu 8 Oct, £9.85"
        assert await fetch_one(conn, "select r.total, l.price from receipts r join receipt_lines l on l.receipt_id = r.id "
                                     "where r.shop = 'Hilltop Grocers' and l.label = 'butter'") == {
            "total": D("9.85"), "price": D("2.45")}
        moved = await tool(ctx, "update_receipt", shop="freshway", bought_on="2026-10-03", new_shop="Lidl",
                           new_date="2026-10-02")
        assert moved == "OK: receipt corrected: Lidl, Fri 2 Oct, £22.69"
        assert (await spending.summary(conn, home.id, date(2026, 10, 1), date(2026, 10, 31))).shops == [
            ("Lidl", D("22.69")), ("Hilltop Grocers", D("9.85")), ("Freshway Supermarket", D("5.80"))]

        gone = await tool(ctx, "update_receipt", shop="Lidl", remove=True)
        assert gone.splitlines() == ["OK: receipt removed from the spending: Lidl, Fri 2 Oct, £22.69",
                                     "NOTE: what it added to the stock is unchanged"]
        assert (await spending.summary(conn, home.id, date(2026, 10, 1), date(2026, 10, 31))).total == D("15.65")
        assert await stock_of(conn, home) == stock_before

        # Each is one change that undo takes back, newest first.
        await tool(ctx, "undo_last", n=3)
        october = await spending.summary(conn, home.id, date(2026, 10, 1), date(2026, 10, 31))
        assert (october.total, october.shops[0]) == (D("37.44"), ("Freshway Supermarket", D("28.49")))
        assert await fetch_val(conn, "select price from receipt_lines l join receipts r on r.id = l.receipt_id "
                                     "where r.shop = 'Hilltop Grocers' and l.label = 'butter'") == D("2.40")


async def test_a_receipt_that_cannot_be_told_apart_or_found_is_not_changed():
    async with tx() as conn:
        home = await seed_home(conn)
        ctx = ctx_for(conn, home, now=NOW)
        nothing, is_error = await run_tool("update_receipt", {"remove": True}, ctx)
        assert is_error and nothing == "ERROR: no receipt like that is kept. No receipt has been kept yet"
        await add_receipt(conn, home, "Freshway Supermarket", "2026-10-06", 5.80, (("milk", 1.65),))
        await add_receipt(conn, home, "Freshway Supermarket", "2026-10-06", 12.40, (("rice", 9.50),))
        assert await tool(ctx, "update_receipt", shop="Freshway", bought_on="2026-10-06", remove=True) == (
            "AMBIGUOUS: that could be Freshway Supermarket, Tue 6 Oct, £12.40; Freshway Supermarket, Tue 6 Oct, £5.80")
        missing, is_error = await run_tool("update_receipt", {"shop": "Waitrose", "remove": True}, ctx)
        assert is_error and "The newest are: Freshway Supermarket, Tue 6 Oct, £12.40; Freshway Supermarket" in missing
        assert len(await receipts(conn, home)) == 2
        assert "£5.80" in await tool(ctx, "update_receipt", shop="Freshway", bought_on="2026-10-06", total=5.80,
                                     remove=True)
        assert [r["total"] for r in await receipts(conn, home)] == [D("12.40")]

        no_line = await tool(ctx, "update_receipt", prices=[{"item": "bread", "price": 1}])
        assert no_line == "ERROR: there is no line for bread on that receipt"
        for wrong in ({"new_date": "2027-01-01"}, {}):
            result, is_error = await run_tool("update_receipt", wrong, ctx)
            assert is_error, result
        assert await fetch_all(conn, "select 1 from agent_actions where tool = 'update_receipt' and undone_at is null "
                                     "and result like 'OK: receipt corrected%'") == []


async def test_merging_a_duplicate_item_keeps_its_receipt_lines():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_receipt(conn, home, "Freshway Supermarket", "2026-10-06", 5.80, (("milk", 1.65), ("semi skimmed milk", 1.70)))
        ids = {row["canonical_name"]: row["id"] for row in await fetch_all(conn, "select id, canonical_name from items")}
        from app.agent.actions import Recorder
        await inventory.merge_items(Recorder(ctx_for(conn, home)), ids["milk"], ids["semi skimmed milk"])
        found = await spending.spent_on(conn, home.id, date(2026, 10, 1), date(2026, 10, 31), "milk", item_id=ids["milk"])
        assert (found.amount, found.receipts) == (D("3.35"), 1)


# ---------------------------------------------------------------- the currency
async def test_the_currency_is_the_households_and_is_changed_by_saying_so():
    assert [spending.money(D(amount), code) for amount, code in (("5.8", "GBP"), ("-1", "GBP"), ("1234.5", "NGN"),
                                                                 ("12.5", "CHF"))] == [
        "£5.80", "-£1.00", "₦1,234.50", "CHF 12.50"]
    assert [spending.currency_code(said) for said in ("eur", " pounds ", "€", "NGN")] == ["EUR", "GBP", "EUR", "NGN"]
    async with tx() as conn:
        home = await seed_home(conn)
        ctx = ctx_for(conn, home, now=NOW)
        assert await spending.currency(conn, home.id) == "GBP"
        await add_receipt(conn, home, "Freshway Supermarket", "2026-10-06", 5.80, (("milk", 1.65),))
        assert await tool(ctx, "remember", key="currency", value="euros") == "OK: amounts are in Euros (€1.50)"
        wrong, is_error = await run_tool("remember", {"key": "currency", "value": "shells"}, ctx)
        assert is_error and "is not a currency" in wrong
        # Receipts keep the currency they were read in, and are not added to sums in another.
        await add_receipt(conn, home, "Carrefour", "2026-10-08", 30, (("bread", 1.40),))
        assert (await tool(ctx, "get_spending")).splitlines() == [
            "OK: spent, 1 Oct to 10 Oct 2026: €30.00 on 1 receipt", "By shop: Carrefour €30.00",
            "By kind: not sorted yet €1.40; not listed line by line €28.60",
            "NOTE: 1 receipt in another currency is not added in"]
        await tool(ctx, "undo_last")
        assert await spending.currency(conn, home.id) == "GBP"


# ---------------------------------------------------------------- the month, with the weekly digest
async def test_the_first_weekly_digest_of_a_month_says_what_was_spent_the_month_before_and_only_once():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_member(conn, home, "Ada", telegram_id="1002")
        await a_month_of_receipts(conn, home)
        await add_receipt(conn, home, "Freshway Supermarket", "2026-11-03", 6.00, (("milk", 1.75, 2, "pints"),))

    async def digests():
        async with tx() as conn:
            return [row["text"] for row in await fetch_all(conn, "select text from outbox order by created_at")]

    assert await jobs.weekly_digest(london("2026-11-01 18:00")) == 1          # the first Sunday of November
    assert (await digests())[-1].splitlines() == [
        "Nothing booked this week.",
        "",
        "Spending in October: £37.44 from 3 receipts.",
        "- Freshway Supermarket £28.49",
        "- Hilltop Grocers £8.95",
        "Most went on cupboard (£9.50), dairy and eggs (£8.99) and household (£5.10).",
        "September was £5.60.",
        "Price change: bread at Freshway Supermarket: £1.20, was £1.10 (up £0.10).",
        "Price change: milk at Freshway Supermarket: £1.65, was £1.55 (up £0.10)."]
    assert await jobs.weekly_digest(london("2026-11-08 18:00")) == 0          # nothing else to say, and October was said
    assert len(await digests()) == 1
    # December's first Sunday was missed: November goes with the next digest that runs, once.
    assert await jobs.weekly_digest(london("2026-12-13 18:00")) == 1
    assert "Spending in November: £6.00 from 1 receipt." in (await digests())[-1]
    assert "Price change: milk at Freshway Supermarket: £1.75, was £1.65 (up £0.10)." in (await digests())[-1]
    assert await jobs.weekly_digest(london("2026-12-20 18:00")) == 0


async def test_a_month_with_no_priced_receipt_adds_nothing_to_the_digest():
    async with tx() as conn:
        home = await seed_home(conn)
        ctx = await a_message(conn, home)
        ctx.now = london("2026-10-06 12:00")
        await tool(ctx, "log_inventory", source="receipt", changes=[{"item": "milk", "action": "restocked"}])
        await execute(conn, "update inventory_events set occurred_at = :at", at=ctx.now)
    assert await spending_report(home, date(2026, 10, 1)) is None
    assert await jobs.weekly_digest(london("2026-11-01 18:00")) == 0
    async with tx() as conn:
        assert await fetch_all(conn, "select 1 from outbox") == []
        await add_receipt(conn, home, "Freshway Supermarket", "2026-10-06", 5.80, (("milk", 1.65),))
    assert (await spending_report(home, date(2026, 10, 1))).splitlines() == [
        "Spending in October: £5.80 from 1 receipt.", "- Freshway Supermarket £5.80",
        "1 more receipt had no prices kept, so the real figure is higher."]


async def spending_report(home, month):
    async with tx() as conn:
        return await spending.month_report(conn, home.id, month)


# ---------------------------------------------------------------- the Spending page
def words(page):
    """A page as the words on it."""
    return " ".join(re.sub(r"<[^>]+>", " ", page).split())


async def test_the_spending_page_shows_a_month_by_shop_and_kind_the_months_and_the_price_changes(client):
    async with tx() as conn:
        home = await seed_home(conn)
        await a_month_of_receipts(conn, home)
    await login(client, home)
    page = words((await client.get("/dashboard/spending?month=2026-10")).text)
    for expected in (
            "October 2026", "£37.44 Spent in October", "from 3 receipts.",
            "By shop Freshway Supermarket £28.49 Hilltop Grocers £8.95",
            "cupboard £9.50 dairy and eggs £8.99 household £5.10", "not sorted yet £1.10",
            "Not listed line by line", "£5.45", "Discounts Taken off at the till. -£0.70",
            "September £5.60", "October £37.44", "August No receipts",
            "bread at Freshway Supermarket: £1.20, was £1.10 (up £0.10)",
            "Hilltop Grocers Thu 8 Oct · 2 prices £8.95", "Take this receipt out"):
        assert expected in page, expected
    assert technical_words(page) == []
    # The page is in the menu, on a phone under More, and another month is one tap away.
    raw = (await client.get("/dashboard/spending?month=2026-10")).text
    assert 'href="/dashboard/spending" aria-current="page"' in raw and 'href="/dashboard/spending?month=2026-09"' in raw
    assert 'href="/dashboard/spending"' in (await client.get("/dashboard/more")).text

    september = words((await client.get("/dashboard/spending?month=2026-09")).text)
    assert "£5.60 Spent in September, from 1 receipt." in september and "Nothing changed price in September" in september
    for address in ("/dashboard/spending?month=2031-01", "/dashboard/spending?month=nonsense", "/dashboard/spending"):
        assert (await client.get(address)).status_code == 200            # never a month that has not come


async def test_the_spending_page_says_when_prices_were_not_kept_and_never_shows_a_zero_for_them(client):
    async with tx() as conn:
        home = await seed_home(conn)
        ctx = await a_message(conn, home)
        await tool(ctx, "log_inventory", source="receipt", changes=[{"item": "milk", "action": "restocked"}])
        await execute(conn, "update inventory_events set occurred_at = :at", at=london("2026-09-12 10:00"))
    await login(client, home)
    page = words((await client.get("/dashboard/spending?month=2026-09")).text)
    assert ("1 receipt was read in September, but its prices were not kept, so what was spent is not known." in page
            and "September Prices not kept" in page and "£0" not in page)
    empty = words((await client.get("/dashboard/spending?month=2026-08")).text)
    assert "No receipts for August." in empty and "£0" not in empty
    async with tx() as conn:
        await add_receipt(conn, home, "Freshway Supermarket", "2026-09-20", 5.60, (("milk", 1.55),))
    page = words((await client.get("/dashboard/spending?month=2026-09")).text)
    assert "1 more receipt was read this month without its prices, so the real figure is higher." in page


async def test_the_spending_page_corrects_and_removes_a_receipt_through_the_service_and_activity_can_undo(client):
    async with tx() as conn:
        home = await seed_home(conn)
        other = await seed_home(conn, telegram_id=None)
        await a_month_of_receipts(conn, home)
        theirs = await add_receipt(conn, other, "Waitrose", "2026-10-07", 60.00, (("caviar", 45.00),))
        hilltop = await fetch_val(conn, "select id from receipts where shop = 'Hilltop Grocers'")
        price_of = {row["label"]: row["id"] for row in await fetch_all(
            conn, "select id, label from receipt_lines where receipt_id = :id", id=hilltop)}
    csrf = await login(client, home)

    edited = await client.post(
        f"/dashboard/spending/receipts/{hilltop}/edit?month=2026-10", headers=csrf,
        data={"shop": " Hilltop  Bakery ", "purchased_on": "2026-10-07", "total": "9.85",
              f"price_{price_of['butter']}": "2.45", f"price_{price_of['coconut milk']}": ""})
    assert edited.status_code == 200 and "Hilltop Bakery Wed 7 Oct · 1 price £9.85" in words(edited.text)
    async with tx() as conn:
        assert await fetch_one(conn, "select shop, purchased_on, total from receipts where id = :id", id=hilltop) == {
            "shop": "Hilltop Bakery", "purchased_on": date(2026, 10, 7), "total": D("9.85")}
        assert await fetch_all(conn, "select label, price from receipt_lines where receipt_id = :id", id=hilltop) == [
            {"label": "butter", "price": D("2.45")}]
        logged = await fetch_one(conn, "select id, source, result from agent_actions where tool = 'spending.edit'")
        assert (logged["source"], logged["result"]) == ("dashboard", "OK: receipt corrected: Hilltop Bakery, Wed 7 Oct, £9.85")

    # No total printed: the receipt counts for its lines. A wrong value changes nothing and says so.
    no_total = await client.post(f"/dashboard/spending/receipts/{hilltop}/edit?month=2026-10", headers=csrf,
                                 data={"shop": "Hilltop Bakery", "purchased_on": "2026-10-07", "total": ""})
    assert "Hilltop Bakery Wed 7 Oct · 1 price · no total on it £2.45" in words(no_total.text)
    for wrong in ({"purchased_on": "2031-01-01", "total": "1"}, {"purchased_on": "2026-10-07", "total": "lots"},
                  {"purchased_on": "", "total": "1"}, {"purchased_on": "2026-10-07", "total": "-1"}):
        refused = await client.post(f"/dashboard/spending/receipts/{hilltop}/edit?month=2026-10", headers=csrf,
                                    data={"shop": "Hilltop Bakery", **wrong})
        assert 'class="error"' in refused.text, wrong
    async with tx() as conn:
        assert await fetch_val(conn, "select total from receipts where id = :id", id=hilltop) is None

    removed = await client.post(f"/dashboard/spending/receipts/{hilltop}/remove?month=2026-10", headers=csrf)
    assert "Hilltop Bakery" not in removed.text and "£28.49 Spent in October" in words(removed.text)
    async with tx() as conn:
        action = await fetch_val(conn, "select id from agent_actions where tool = 'spending.remove'")
    assert "Undone" in (await client.post(f"/dashboard/activity/actions/{action}/undo", headers=csrf)).text
    await client.post(f"/dashboard/activity/actions/{logged['id']}/undo", headers=csrf)   # refused: a later edit stands
    assert "Hilltop Bakery" in (await client.get("/dashboard/spending?month=2026-10")).text

    # Another household's receipt is not shown and cannot be changed, and a POST needs the token.
    assert "Waitrose" not in (await client.get("/dashboard/spending?month=2026-10")).text
    for verb, data in (("remove", {}), ("edit", {"shop": "Ours", "purchased_on": "2026-10-07", "total": "1"})):
        refused = await client.post(f"/dashboard/spending/receipts/{theirs}/{verb}", headers=csrf, data=data)
        assert "that receipt is no longer kept" in refused.text
    assert (await client.post(f"/dashboard/spending/receipts/{hilltop}/remove")).status_code == 403
    async with tx() as conn:
        assert await fetch_one(conn, "select shop, total, status from receipts where id = :id", id=theirs) == {
            "shop": "Waitrose", "total": D("60.00"), "status": "kept"}


async def test_settings_changes_the_currency_and_the_export_holds_the_receipts(client):
    async with tx() as conn:
        home = await seed_home(conn)
        await add_receipt(conn, home, "Freshway Supermarket", "2026-10-06", 5.80, (("milk", 1.65),))
    csrf = await login(client, home)
    assert '<option value="GBP" selected>British pounds</option>' in (await client.get("/dashboard/settings")).text
    changed = await client.post("/dashboard/settings/currency", data={"currency": "NGN"}, headers=csrf)
    assert '<option value="NGN" selected>Nigerian naira</option>' in changed.text
    async with tx() as conn:
        assert await spending.currency(conn, home.id) == "NGN"
        assert await fetch_val(conn, "select currency from receipts") == "GBP"      # as it was read
    refused = await client.post("/dashboard/settings/currency", data={"currency": "money"}, headers=csrf)
    assert "is not a currency" in refused.text
    export = (await client.get("/dashboard/system/export")).json()["tables"]
    assert [(r["shop"], r["total"]) for r in export["receipts"]] == [("Freshway Supermarket", 5.8)]
    assert [(line["label"], line["price"]) for line in export["receipt_lines"]] == [("milk", 1.65)]
