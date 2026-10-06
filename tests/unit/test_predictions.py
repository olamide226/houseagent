"""The consumption model and what follows from it (spec section 10): profiles learned from seeded
event history on a controlled clock, the list's "probably" entries, the nightly job, the 17:30
low-stock prompt and the weekly digest's predicted items."""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from app.agent.loop import LoopRuntime
from app.agent.tools import run_tool
from app.core.envelope import Channel
from app.db import execute, fetch_all, tx
from app.pipeline import inbound, router
from app.services import consumption
from app.services.consumption import Profile, profile
from app.worker import jobs
from tests.helpers import (
    FakeAdapter,
    FakeLLM,
    active_list,
    add_item,
    add_member,
    bought_every,
    call,
    ctx_for,
    events_of,
    history,
    london,
    say,
    seed_home,
    tg_update,
)

T0 = datetime(2026, 9, 1, 9, 0)
SECRET = {"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"}


def days(n: float) -> datetime:
    return T0 + timedelta(days=n)


def cycles(*lengths: float) -> list[tuple[str, datetime]]:
    """Back-to-back restock-to-finished cycles of these lengths in days, then one more restock."""
    events, at = [], 0.0
    for length in lengths:
        events += [("restocked", days(at)), ("finished", days(at + length))]
        at += length
    return [*events, ("restocked", days(at))]


async def rows(sql, **params):
    async with tx() as conn:
        return await fetch_all(conn, sql, **params)


async def tool(home, name, **args):
    async with tx() as conn:
        result, is_error = await run_tool(name, args, ctx_for(conn, home))
        assert not is_error, result
        return result


async def refresh(home, at):
    async with tx() as conn:
        await consumption.refresh(conn, home.id, london(at))
        return await active_list(conn, home)


# ---------------------------------------------------------------- the model, as a pure function
def test_no_history_means_no_profile():
    assert profile([]) == Profile(None, 0, None, None)


def test_one_cycle_is_not_enough_to_predict_and_two_are():
    assert profile(cycles(6)) == Profile(6.0, 1, days(6), None)
    assert profile(cycles(6, 4)) == Profile(5.0, 2, days(10), days(15))


def test_the_average_weights_the_newest_cycle_most():
    assert profile(cycles(2, 2, 10)).avg_days == 6.0          # 2, then 2, then (10 + 2) / 2
    assert profile(cycles(10, 2, 2)).avg_days == 4.0          # 10, then 6, then (2 + 6) / 2
    assert profile(cycles(6, 4, 6)) == Profile(5.5, 3, days(16), days(21.5))


@pytest.mark.parametrize("hours,counted", [(11.99, 0), (12, 1), (24 * 120, 1), (24 * 120 + 0.01, 0)])
def test_cycles_under_twelve_hours_or_over_120_days_are_ignored(hours, counted):
    assert profile(cycles(hours / 24)).samples == counted


@pytest.mark.parametrize("end", ["finished", "low", "discarded"])
def test_finished_low_and_discarded_each_end_a_cycle(end):
    learned = profile([("restocked", days(0)), (end, days(3)), ("added", days(4)), (end, days(9))])
    assert (learned.avg_days, learned.samples) == (4.0, 2)
    assert learned.predicted_runout_at is None                # it has run out and was not bought again


def test_an_end_with_nothing_bought_since_counts_for_nothing():
    learned = profile([("restocked", days(0)), ("low", days(5)), ("finished", days(7)), ("finished", days(50))])
    assert (learned.avg_days, learned.samples) == (5.0, 1)


def test_buying_more_before_it_runs_out_restarts_the_cycle():
    learned = profile([("restocked", days(0)), ("restocked", days(7)), ("finished", days(9)),
                       ("restocked", days(10)), ("finished", days(14)), ("restocked", days(20)),
                       ("restocked", days(22))])
    assert learned == Profile(3.0, 2, days(22), days(25))      # cycles of 2 and 4 days, counted from the last buy


def test_events_that_are_not_a_purchase_or_a_run_out_change_nothing():
    plain = cycles(6, 4)
    noisy = [plain[0], ("used", days(1)), ("adjusted", days(2)), *plain[1:], ("used", days(11))]
    assert profile(noisy) == profile(plain)


# ---------------------------------------------------------------- learned from the event log
async def milk_and_friends(conn):
    """Milk: cycles of 6, 4 and 6 days, last bought Fri 2 Oct 18:00, so it runs out Thu 8 Oct 06:00."""
    home = await seed_home(conn)
    await add_member(conn, home, "Ada", telegram_id="1002")
    milk = await add_item(conn, home, "milk", location="fridge")
    await history(conn, home, milk,
                  ("restocked", "2026-09-14 09:00"), ("finished", "2026-09-20 09:00"),
                  ("restocked", "2026-09-21 09:00"), ("finished", "2026-09-25 09:00"),
                  ("added", "2026-09-26 09:00"), ("low", "2026-10-02 09:00"),
                  ("restocked", "2026-10-02 18:00"))
    return home, milk


async def test_profiles_are_learned_from_seeded_history_and_only_an_item_in_stock_is_predicted():
    async with tx() as conn:
        home, milk = await milk_and_friends(conn)
        bread = await add_item(conn, home, "bread")                      # one cycle: not enough
        await history(conn, home, bread, ("restocked", "2026-09-20 09:00"), ("finished", "2026-09-24 09:00"),
                      ("restocked", "2026-09-25 09:00"))
        rice = await add_item(conn, home, "rice")                        # two cycles, but it is out now
        await history(conn, home, rice, ("restocked", "2026-08-01 09:00"), ("finished", "2026-08-21 09:00"),
                      ("restocked", "2026-08-22 09:00"), ("finished", "2026-09-21 09:00"))
        await add_item(conn, home, "salt", qty=1)                        # never bought or finished
        await consumption.refresh(conn, home.id, london("2026-10-05 12:00"))

    learned = {r["item"]: r for r in await rows(
        "select i.canonical_name as item, p.* from consumption_profiles p join items i on i.id = p.item_id")}
    assert set(learned) == {"milk", "bread", "rice"}
    assert (learned["milk"]["avg_days_to_finish"], learned["milk"]["samples"]) == (D("5.5"), 3)
    assert learned["milk"]["last_restocked_at"] == london("2026-10-02 18:00")
    assert learned["milk"]["predicted_runout_at"] == london("2026-10-08 06:00")
    assert learned["milk"]["updated_at"] == london("2026-10-05 12:00")
    assert (learned["bread"]["avg_days_to_finish"], learned["bread"]["samples"]) == (D("4"), 1)
    assert learned["bread"]["predicted_runout_at"] is None
    assert (learned["rice"]["avg_days_to_finish"], learned["rice"]["samples"]) == (D("25"), 2)
    assert learned["rice"]["predicted_runout_at"] is None


async def test_an_undone_event_is_not_learned_from():
    async with tx() as conn:
        home, _ = await milk_and_friends(conn)
    await tool(home, "log_inventory", changes=[{"item": "milk", "action": "finished"}])
    # Told it has run out, there is nothing left to predict. (Two finished cycles made milk a staple,
    # which is why it is on the list for real.)
    assert await refresh(home, "2026-10-07 12:00") == {"milk": "finished"}
    assert (await rows("select predicted_runout_at from consumption_profiles"))[0]["predicted_runout_at"] is None

    await tool(home, "undo_last")
    assert await refresh(home, "2026-10-07 12:00") == {"milk": "predicted"}
    (learned,) = await rows("select samples, predicted_runout_at from consumption_profiles")
    assert learned == {"samples": 3, "predicted_runout_at": london("2026-10-08 06:00")}

    async with tx() as conn:   # and an item whose every event was undone has no profile at all
        await execute(conn, "delete from inventory_events")
    assert await refresh(home, "2026-10-07 12:00") == {}
    assert await rows("select 1 from consumption_profiles") == []


# ---------------------------------------------------------------- "probably" entries on the list
async def test_an_item_goes_on_the_list_as_a_guess_two_days_before_it_is_predicted_to_run_out():
    async with tx() as conn:
        home, _ = await milk_and_friends(conn)

    assert await refresh(home, "2026-10-05 12:00") == {}
    assert await refresh(home, "2026-10-06 05:59") == {}
    assert await refresh(home, "2026-10-06 06:00") == {"milk": "predicted"}
    assert await refresh(home, "2026-10-06 06:00") == {"milk": "predicted"}     # run twice: still one entry
    (entry,) = await rows("select reason, status, added_by, added_at from shopping_list_items")
    assert entry == {"reason": "predicted", "status": "needed", "added_by": None,
                     "added_at": london("2026-10-06 06:00")}
    assert "- milk (probably)" in await tool(home, "get_shopping_list")
    assert "milk" not in await tool(home, "get_shopping_list", include_predicted=False)

    # A week after the predicted day with no word either way, the guess was wrong: it is dropped for good.
    assert await refresh(home, "2026-10-15 06:00") == {"milk": "predicted"}
    assert await refresh(home, "2026-10-15 06:01") == {}
    assert await refresh(home, "2026-10-15 06:02") == {}
    assert [(r["reason"], r["status"]) for r in await rows("select reason, status from shopping_list_items")] == [
        ("predicted", "dismissed")]


async def test_asking_for_a_guessed_item_makes_it_a_real_entry_and_undo_makes_it_a_guess_again():
    async with tx() as conn:
        home, _ = await milk_and_friends(conn)
    await refresh(home, "2026-10-07 12:00")

    assert await tool(home, "update_shopping_list", add=[{"item": "milk", "store_hint": "Tesco"}]) == (
        "OK: milk added to shopping list")
    (entry,) = await rows("select reason, store_hint, added_by from shopping_list_items")
    assert entry == {"reason": "explicit", "store_hint": "Tesco", "added_by": home.ola}
    assert await refresh(home, "2026-10-07 12:00") == {"milk": "explicit"}     # the model leaves a real entry alone

    await tool(home, "undo_last")
    (entry,) = await rows("select reason, store_hint, added_by, status from shopping_list_items")
    assert entry == {"reason": "predicted", "store_hint": None, "added_by": None, "status": "needed"}


@pytest.mark.parametrize("action,reason", [("low", "low"), ("finished", "finished")])
async def test_being_told_a_guessed_item_is_low_or_finished_makes_the_entry_real(action, reason):
    async with tx() as conn:
        home, milk = await milk_and_friends(conn)
        await execute(conn, "update items set is_staple = true where id = :id", id=milk)
    await refresh(home, "2026-10-07 12:00")
    assert "NOTE: milk added to shopping list" in await tool(
        home, "log_inventory", changes=[{"item": "milk", "action": action}])
    async with tx() as conn:
        assert await active_list(conn, home) == {"milk": reason}
    assert await refresh(home, "2026-10-07 13:00") == {"milk": reason}


async def test_buying_a_guessed_item_clears_it_and_taking_it_off_keeps_it_off_until_it_is_bought_again():
    async with tx() as conn:
        home, milk = await milk_and_friends(conn)
    await refresh(home, "2026-10-07 12:00")

    await tool(home, "update_shopping_list", remove=["milk"])                # "no, we have plenty"
    async with tx() as conn:
        await execute(conn, "update shopping_list_items set resolved_at = :at", at=london("2026-10-07 12:05"))
    assert await refresh(home, "2026-10-07 18:00") == {}
    assert await refresh(home, "2026-10-08 18:00") == {}

    # Bought again on the 12th. That cycle lasted 8.6 days, so the average is now 7.06 and the next
    # run-out is Mon 19 Oct 10:30: a guess again from two days before.
    async with tx() as conn:
        await history(conn, home, milk, ("finished", "2026-10-11 09:00"), ("restocked", "2026-10-12 09:00"))
    assert await refresh(home, "2026-10-16 12:00") == {}
    assert await refresh(home, "2026-10-17 12:00") == {"milk": "predicted"}

    assert "NOTE: milk ticked off the shopping list" in await tool(
        home, "log_inventory", changes=[{"item": "milk", "action": "restocked"}])
    assert [r["status"] for r in await rows("select status from shopping_list_items order by added_at")] == [
        "dismissed", "bought"]


async def test_got_everything_buys_what_was_asked_for_and_leaves_the_guesses():
    async with tx() as conn:
        home, _ = await milk_and_friends(conn)
    await refresh(home, "2026-10-07 12:00")
    await tool(home, "update_shopping_list", add=[{"item": "eggs"}])
    await tool(home, "update_shopping_list", bought_all=True)
    async with tx() as conn:
        assert await active_list(conn, home) == {"milk": "predicted"}
        assert [(item, kind) for item, kind, _, _ in await events_of(conn, home)][-1] == ("egg", "restocked")
        assert len(await events_of(conn, home)) == 8           # milk's seven, and the eggs


async def test_the_list_for_a_shop_has_what_names_it_loosely_and_what_names_no_shop():
    async with tx() as conn:
        home = await seed_home(conn)
    await tool(home, "update_shopping_list", add=[
        {"item": "yam", "store_hint": "African shop"}, {"item": "bin bags", "store_hint": "Costco"},
        {"item": "bleach", "store_hint": "tesco"}, {"item": "bread"}])

    async def at(store):
        return sorted(line[2:].split(" [")[0] for line in (await tool(home, "get_shopping_list", store=store)).splitlines())

    assert await at("Tesco Extra") == ["bleach", "bread"]
    assert await at("the African shop on Rye Lane") == ["bread", "yam"]
    assert await at("Costco") == ["bin bag", "bread"]
    assert await at("Lidl") == ["bread"]


# ---------------------------------------------------------------- the nightly job
async def test_the_nightly_model_runs_once_a_day_from_three_and_late_rather_than_never():
    async with tx() as conn:
        home, _ = await milk_and_friends(conn)

    assert await jobs.consumption_model(now=london("2026-10-07 02:59")) == 0
    assert await rows("select 1 from consumption_profiles") == []
    ran = await asyncio.gather(*(jobs.consumption_model(now=london("2026-10-07 03:00")) for _ in range(4)))
    assert sum(ran) == 1                                        # four workers, one run
    assert await jobs.consumption_model(now=london("2026-10-07 15:00")) == 0
    async with tx() as conn:
        assert await active_list(conn, home) == {"milk": "predicted"}
    assert [r["run_key"] for r in await rows("select run_key from job_runs where job = 'consumption_model'")] == [
        "2026-10-07"]
    assert await rows("select 1 from outbox") == []            # it says nothing by itself

    # The worker was down overnight and came back at ten: the model still runs for that day.
    assert await jobs.consumption_model(now=london("2026-10-08 10:00")) == 1


async def test_the_nightly_job_gives_uncategorised_items_a_category_in_one_model_call():
    async with tx() as conn:
        home, _ = await milk_and_friends(conn)
        for name in ("bleach", "yam", "mystery tin", "forgotten"):
            await add_item(conn, home, name)
        await add_item(conn, home, "nappies")
        await execute(conn, "update items set category = 'baby' where canonical_name = 'nappies'")
        other = await seed_home(conn, telegram_id="2001")
        await add_item(conn, other, "yam")
        await execute(conn, "update items set category = 'cupboard' where household_id = :h", h=other.id)
    llm = FakeLLM(say('```json\n{"milk": "dairy and eggs", "bleach": "household", "yam": "fruit and veg", '
                      '"mystery tin": "tinned oddities", "nappies": "household", "caviar": "meat and fish"}\n```'))

    assert await jobs.consumption_model(llm, london("2026-10-07 03:00")) == 2
    (system, messages, tools), = llm.requests                   # one call: the other household had nothing to sort
    assert "dairy and eggs" in system and tools == []
    # Oldest first; these were all made in one transaction, so by name.
    assert messages[0].content[0].text == '["bleach", "forgotten", "milk", "mystery tin", "yam"]'
    assert {(r["canonical_name"], r["category"]) for r in await rows(
        "select canonical_name, category from items where household_id = :h", h=home.id)} == {
        ("milk", "dairy and eggs"), ("bleach", "household"), ("yam", "fruit and veg"),
        ("mystery tin", "other"),                               # a section that does not exist
        ("forgotten", None),                                    # left out of the answer: asked again tomorrow
        ("nappies", "baby"),                                    # already had one
    }
    assert await rows("select 1 from items where canonical_name = 'caviar'") == []
    assert (await rows("select category from items where household_id = :h", h=other.id)) == [{"category": "cupboard"}]


@pytest.mark.parametrize("answer", [say("Sorry, I can't sort those."), say('["dairy"]'), RuntimeError("model down")])
async def test_a_model_that_fails_or_rambles_leaves_categories_alone_and_the_model_still_runs(answer):
    class Broken(FakeLLM):
        async def complete(self, *args, **kwargs):
            if isinstance(answer, Exception):
                raise answer
            return answer

    async with tx() as conn:
        home, _ = await milk_and_friends(conn)
    assert await jobs.consumption_model(Broken(), london("2026-10-07 03:00")) == 1
    assert await rows("select category from items") == [{"category": None}]
    if not isinstance(answer, Exception):                       # an answer that is not the JSON asked for is no error
        assert await jobs.categorise(Broken(), home.id) == 0
    async with tx() as conn:
        assert await active_list(conn, home) == {"milk": "predicted"}


# ---------------------------------------------------------------- the 17:30 prompt
async def running_low(conn):
    """Bread runs out Wed 7 Oct, milk Thu 8 Oct 06:00. Eggs would too but are on the list; rice lasts till the 20th."""
    home, _ = await milk_and_friends(conn)
    await bought_every(conn, home, "bread", 4, "2026-10-03 09:00")
    await bought_every(conn, home, "rice", 30, "2026-09-20 09:00")
    eggs = await bought_every(conn, home, "egg", 5, "2026-10-02 12:00")
    await execute(conn, "insert into shopping_list_items (household_id, item_id, reason) values (:h, :i, 'explicit')",
                  h=home.id, i=eggs)
    return home


async def test_the_low_stock_prompt_asks_once_at_half_past_five_about_what_is_predicted_to_run_out():
    async with tx() as conn:
        await running_low(conn)

    assert await jobs.low_stock_prompt(london("2026-10-06 17:29")) == 0
    assert await rows("select 1 from job_runs") == []
    asked = await asyncio.gather(*(jobs.low_stock_prompt(london("2026-10-06 17:30")) for _ in range(4)))
    assert sum(asked) == 1
    assert await jobs.low_stock_prompt(london("2026-10-06 17:31")) == 0

    (prompt,) = await rows("select text, target, urgency, respect_quiet_hours, dedupe_key from outbox")
    assert prompt == {"text": "Probably running low: bread, milk. Add to the list?", "target": "household",
                      "urgency": "low", "respect_quiet_hours": True, "dedupe_key": "low_stock_prompt:2026-10-06"}
    adapter = FakeAdapter()
    while await router.dispatch_due({Channel.telegram: adapter}, now=london("2026-10-06 17:30")):
        pass
    assert sorted(adapter.sent) == [(chat, "Probably running low: bread, milk. Add to the list?", None)
                                    for chat in ("1001", "1002")]


async def test_nobody_is_asked_about_the_same_item_twice_in_three_days():
    async with tx() as conn:
        home = await running_low(conn)
        salt = await bought_every(conn, home, "salt", 6, "2026-10-03 12:00")    # runs out Fri 9 Oct 12:00

    async def asked(at):
        before = len(await rows("select 1 from outbox"))
        await jobs.low_stock_prompt(london(at))
        return [r["text"] for r in await rows("select text from outbox order by created_at")][before:]

    assert await asked("2026-10-06 17:30") == ["Probably running low: bread, milk. Add to the list?"]
    assert await asked("2026-10-07 17:30") == ["Probably running low: salt. Add to the list?"]   # new today
    assert await asked("2026-10-08 17:30") == []
    assert await asked("2026-10-09 17:30") == ["Probably running low: bread, milk. Add to the list?"]
    assert await asked("2026-10-10 17:30") == ["Probably running low: salt. Add to the list?"]
    async with tx() as conn:   # bought: nothing more to ask about bread
        await history(conn, home, (await rows("select id from items where canonical_name = 'bread'"))[0]["id"],
                      ("restocked", "2026-10-11 09:00"))
        await history(conn, home, salt, ("finished", "2026-10-11 09:00"))
    assert await asked("2026-10-12 17:30") == ["Probably running low: milk. Add to the list?"]
    # Milk's day was the 8th. A week on with no answer the guess is dropped, so the asking stops.
    assert await asked("2026-10-15 17:30") == []
    assert await asked("2026-10-17 17:30") == []
    assert await asked("2026-10-18 17:30") == ["Probably running low: rice. Add to the list?"]   # due on the 20th


async def test_no_prompt_with_nothing_to_ask_and_none_sent_late():
    async with tx() as conn:
        home = await seed_home(conn)
        await bought_every(conn, home, "rice", 30, "2026-09-20 09:00")
    assert await jobs.low_stock_prompt(london("2026-10-06 17:30")) == 0
    assert await rows("select 1 from outbox") == []

    async with tx() as conn:
        await bought_every(conn, home, "bread", 4, "2026-10-04 09:00")
    assert await jobs.low_stock_prompt(london("2026-10-07 21:31")) == 0         # down until after 21:30
    assert await rows("select 1 from outbox") == [] and await rows("select 1 from nudge_log") == []


async def test_yes_to_the_prompt_reaches_the_agent_with_the_question_in_its_thread(client):
    async with tx() as conn:
        home = await running_low(conn)
    await jobs.low_stock_prompt(london("2026-10-06 17:30"))
    adapters = {Channel.telegram: FakeAdapter()}
    while await router.dispatch_due(adapters, now=london("2026-10-06 17:30")):
        pass

    await client.post("/webhooks/telegram", json=tg_update(1, "yes please"), headers=SECRET)
    llm = FakeLLM(call("update_shopping_list", add=[{"item": "milk"}, {"item": "bread"}]), say("Added both."))
    await inbound.process_household(home.id, LoopRuntime(llm), adapters)

    _, messages, _ = llm.requests[0]
    assert [(m.role, m.content[0].text) for m in messages] == [
        ("assistant", "Probably running low: bread, milk. Add to the list?"), ("user", "yes please")]
    async with tx() as conn:
        assert await active_list(conn, home) == {"milk": "explicit", "bread": "explicit", "egg": "explicit"}


# ---------------------------------------------------------------- the weekly digest
async def test_the_weekly_digest_names_what_will_probably_run_low_in_the_week_ahead():
    async with tx() as conn:
        home = await running_low(conn)          # bread ran out on the 7th and milk on the 8th, with no word
        await bought_every(conn, home, "salt", 6, "2026-10-09 12:00")            # Thu 15 Oct
        await bought_every(conn, home, "oil", 20, "2026-09-29 18:00")            # Mon 19 Oct, 18:00: a week and a bit
        await execute(conn, "delete from inventory_events where item_id in "
                            "(select id from items where canonical_name = 'bread')")

    assert await jobs.weekly_digest(london("2026-10-11 18:00")) == 1
    (digest,) = await rows("select text from outbox")
    assert "Probably running low this week: milk, salt." in digest["text"]
    assert "Shopping list: 1 item." in digest["text"]          # the eggs, which are not also "probably"
    for absent in ("rice", "oil", "egg,", "bread"):
        assert absent not in digest["text"]
