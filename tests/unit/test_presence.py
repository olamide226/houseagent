"""Presence (spec section 11): the Shortcut endpoint, its personal tokens, and the three rules.

Covers the acceptance item "a store-arrival Shortcut call produces the filtered list within 30 s,
at most once per 2 h per store". Shortcut calls are plain HTTP requests, as a phone makes them.
"""
import asyncio
import re
import time

import pytest

from app.config import get_settings
from app.core.envelope import Channel
from app.db import execute, fetch_all, fetch_val, tx
from app.presence import routes
from app.services import members
from app.worker import jobs
from tests.helpers import FakeAdapter, add_item, add_member, london, seed_home

OLD = london("2026-10-01 09:00")           # when list entries were added, unless a test says otherwise


@pytest.fixture
def clock(monkeypatch):
    """The endpoint's clock, pinned and movable: clock("2026-10-06 13:00")."""
    state = {"now": london("2026-10-06 11:00")}
    monkeypatch.setattr(routes, "utcnow", lambda: state["now"])
    return lambda text: state.update(now=london(text))


async def household(conn, name="Adebayo", ola="1001", ada="1002"):
    """Ola and Ada with a presence link each, their child, two shops and home."""
    home = await seed_home(conn, telegram_id=ola)
    await add_member(conn, home, "Ada", telegram_id=ada)
    await add_member(conn, home, "Tobi", role="child")
    for place, kind in (("Tesco Extra", "store"), ("African shop on Rye Lane", "store"), ("Home", "home")):
        await execute(conn, "insert into places (household_id, name, kind) values (:h, :name, :kind)",
                      h=home.id, name=place, kind=kind)
    tokens = {who: await members.new_presence_token(conn, home.members[who]) for who in ("Ola", "Ada")}
    return home, tokens


async def on_list(conn, home, name, *, shop=None, reason="explicit", added_at=OLD):
    item_id = await add_item(conn, home, name)
    await execute(conn, "insert into shopping_list_items (household_id, item_id, reason, store_hint, added_at) "
                        "values (:h, :i, :reason, :shop, :at)", h=home.id, i=item_id, reason=reason, shop=shop,
                  at=added_at)


async def the_usual_list(conn, home):
    await on_list(conn, home, "eggs")
    await on_list(conn, home, "bleach", shop="Tesco")
    await on_list(conn, home, "yam", shop="African shop")
    await on_list(conn, home, "milk", reason="predicted")


async def ping(client, token, event, place):
    response = await client.post(f"/presence/{token}", json={"event": event, "place": place})
    assert (response.status_code, response.content) == (204, b"")


async def rows(sql, **params):
    async with tx() as conn:
        return await fetch_all(conn, sql, **params)


async def nudges():
    """Every queued send, oldest first, as (member name, text)."""
    return [(r["name"], r["text"]) for r in await rows(
        "select m.name, o.text from outbox o join members m on m.id = o.member_id order by o.created_at")]


async def pings():
    return [(r["name"], r["place"], r["event"]) for r in await rows(
        """select m.name, p.name as place, e.event from presence_events e join members m on m.id = e.member_id
           join places p on p.id = e.place_id order by e.occurred_at, e.id""")]


TESCO_LIST = "You're at Tesco Extra. On the list:\n- bleach [Tesco]\n- eggs\n\n- milk (probably)"


# ---------------------------------------------------------------- the acceptance item
async def test_arriving_at_a_shop_sends_that_shops_list_to_whoever_arrived_well_within_30_seconds(client):
    async with tx() as conn:
        home, tokens = await household(conn)
        await the_usual_list(conn, home)
        # Whatever the time of day this runs at, Ola is in quiet hours: the nudge is urgent and goes anyway.
        await execute(conn, "update members set quiet_start = '00:00', quiet_end = '23:59:59'")
    adapter = FakeAdapter()
    worker = asyncio.create_task(jobs.outbox_job({Channel.telegram: adapter}, asyncio.Event()))
    await asyncio.sleep(0.3)                   # the real outbox job has found nothing and is waiting out its poll

    called = time.monotonic()
    await ping(client, tokens["Ola"], "enter", "Tesco Extra")
    try:
        # Sent, and the send committed: stopping the job any earlier would roll its transaction back.
        while await rows("select 1 from outbox where status = 'sent'") == []:
            assert time.monotonic() - called < 30, "no list within 30 seconds of arriving"
            await asyncio.sleep(0.05)
        took = time.monotonic() - called
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    assert took < 5                            # the outbox is polled every 2 s
    assert adapter.sent == [("1001", TESCO_LIST, None)]          # Ola's own chat; the yam is for another shop
    assert await pings() == [("Ola", "Tesco Extra", "enter")]
    (sent,) = await rows("select target, urgency, status from outbox")
    assert sent == {"target": "member", "urgency": "high", "status": "sent"}


async def test_a_second_arrival_inside_two_hours_sends_nothing_and_one_after_two_hours_sends_again(client, clock):
    async with tx() as conn:
        home, tokens = await household(conn)
        await the_usual_list(conn, home)

    clock("2026-10-06 11:00")
    await ping(client, tokens["Ola"], "enter", "Tesco Extra")
    await ping(client, tokens["Ola"], "enter", "Tesco Extra")                # the phone fired twice
    await ping(client, tokens["Ola"], "exit", "Tesco Extra")
    clock("2026-10-06 12:59")
    await ping(client, tokens["Ola"], "enter", "Tesco Extra")                # back from the car park
    assert await nudges() == [("Ola", TESCO_LIST)]
    assert len(await pings()) == 4                                           # every ping is still recorded

    # The two hours are per person and per shop.
    await ping(client, tokens["Ola"], "enter", "African shop on Rye Lane")
    await ping(client, tokens["Ada"], "enter", "Tesco Extra")
    african = "You're at African shop on Rye Lane. On the list:\n- eggs\n- yam [African shop]\n\n- milk (probably)"
    assert await nudges() == [("Ola", TESCO_LIST), ("Ola", african), ("Ada", TESCO_LIST)]

    clock("2026-10-06 13:00")                                                # two hours to the minute
    await ping(client, tokens["Ola"], "enter", "Tesco Extra")
    assert await nudges() == [("Ola", TESCO_LIST), ("Ola", african), ("Ada", TESCO_LIST), ("Ola", TESCO_LIST)]
    clock("2026-10-06 14:59")
    await ping(client, tokens["Ola"], "enter", "Tesco Extra")                # counted from the last list sent
    assert len(await nudges()) == 4


async def test_the_same_call_replayed_many_times_at_once_sends_one_list(client):
    async with tx() as conn:
        home, tokens = await household(conn)
        await the_usual_list(conn, home)
    await asyncio.gather(*(ping(client, tokens["Ola"], "enter", "Tesco Extra") for _ in range(8)))
    assert await nudges() == [("Ola", TESCO_LIST)]
    assert len(await pings()) == 8
    assert len(await rows("select 1 from nudge_log")) == 1


async def test_a_wrong_or_replaced_token_is_answered_the_same_and_does_nothing(client):
    async with tx() as conn:
        home, tokens = await household(conn)
        await the_usual_list(conn, home)
        replaced = await members.new_presence_token(conn, home.ola)          # Ola's link was rotated
        await execute(conn, "update members set presence_token_hash = 'not-a-hash' where name = 'Tobi'")

    for token in ("nonsense", tokens["Ola"], tokens["Ola"][:-1], "not-a-hash"):
        await ping(client, token, "enter", "Tesco Extra")
        await ping(client, token, "enter", "Somewhere new")
    assert await pings() == [] and await nudges() == []
    assert len(await rows("select 1 from places")) == 3                      # and no place was made up

    await ping(client, replaced, "enter", "Tesco Extra")
    assert await nudges() == [("Ola", TESCO_LIST)]
    # Only the hash of a token is kept.
    hashes = [r["presence_token_hash"] for r in await rows("select presence_token_hash from members")]
    assert replaced not in hashes and tokens["Ada"] not in hashes


async def test_a_child_never_has_a_working_link(client):
    async with tx() as conn:
        home, _ = await household(conn)
        await the_usual_list(conn, home)
        token = await members.new_presence_token(conn, home.members["Tobi"])   # not reachable from any page
    await ping(client, token, "enter", "Tesco Extra")
    assert await pings() == [] and await nudges() == []


@pytest.mark.parametrize("seed", ["empty", "only guesses", "only for another shop"])
async def test_nothing_to_buy_at_this_shop_sends_nothing_and_does_not_use_up_the_two_hours(client, seed):
    async with tx() as conn:
        home, tokens = await household(conn)
        if seed == "only guesses":
            await on_list(conn, home, "milk", reason="predicted")
        if seed == "only for another shop":
            await on_list(conn, home, "yam", shop="African shop")
            await on_list(conn, home, "bin bags", shop="Costco")

    await ping(client, tokens["Ola"], "enter", "Tesco Extra")
    assert await nudges() == [] and await rows("select 1 from nudge_log") == []
    assert await pings() == [("Ola", "Tesco Extra", "enter")]

    async with tx() as conn:                   # Ada asks for eggs while Ola is still in the car park
        await on_list(conn, home, "eggs")
    await ping(client, tokens["Ola"], "enter", "Tesco Extra")
    assert [who for who, _ in await nudges()] == ["Ola"]


# ---------------------------------------------------------------- the endpoint
@pytest.mark.parametrize("body", [
    b"", b"not json", b"[]", b'"enter"', b"{}", b'{"event": "enter"}', b'{"place": "Tesco Extra"}',
    b'{"event": "arrive", "place": "Tesco Extra"}', b'{"event": "enter", "place": "   "}',
    b'{"event": "enter", "place": "' + b"x" * 81 + b'"}', b'{"event": null, "place": null}',
])
async def test_a_body_that_is_not_an_enter_or_exit_at_a_named_place_is_answered_204_and_ignored(client, body):
    async with tx() as conn:
        home, tokens = await household(conn)
        await the_usual_list(conn, home)
    response = await client.post(f"/presence/{tokens['Ola']}", content=body)
    assert response.status_code == 204
    assert await pings() == [] and await nudges() == [] and len(await rows("select 1 from places")) == 3


# ---------------------------------------------------------------- the link, opened in a browser
SHORTCUT = "https://www.icloud.com/shortcuts/0123456789abcdef"
ENGINEERS_WORDS = ("POST", "JSON", "request body", "endpoint", "token", "URL", "Method Not Allowed")


def words_of(response):
    """What the page says to someone reading it: the HTML without its tags and their links."""
    return " ".join(re.sub(r"<[^>]+>", " ", response.text).split())


async def test_opening_the_link_shows_what_it_is_for_in_plain_words_and_records_nothing(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "presence_shortcut_url", SHORTCUT)
    async with tx() as conn:
        home, tokens = await household(conn)
        await the_usual_list(conn, home)
    tesco = f"http://testserver/presence/{tokens['Ada']}/enter/Tesco%20Extra"

    for opened in (f"/presence/{tokens['Ada']}", tesco):                     # her link, and one copied from the page
        page = await client.get(opened)
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
        said = words_of(page)
        assert "lets your iPhone tell Home when you arrive at a shop" in said and "It is optional" in said
        assert not [word for word in ENGINEERS_WORDS if word in said], said
        # One thing to copy per shop, the shared shortcut to add it to, and the two for home.
        assert f'data-copy="{tesco}"' in page.text and "African%20shop%20on%20Rye%20Lane" in page.text
        assert f'href="{SHORTCUT}"' in page.text and "Get Shortcut" in said
        assert f"{tokens['Ada']}/exit/Home" in page.text
        # The address holds a secret: it is not cached, indexed, or passed on to the page a link opens.
        assert (page.headers["cache-control"], page.headers["referrer-policy"]) == ("no-store", "no-referrer")
    assert await pings() == [] and await nudges() == [] and len(await rows("select 1 from places")) == 3


async def test_until_the_shortcut_is_shared_the_admin_is_shown_how_and_everyone_else_is_told_to_wait(client):
    async with tx() as conn:
        _, tokens = await household(conn)
    ada, ola = [words_of(await client.get(f"/presence/{tokens[who]}")) for who in ("Ada", "Ola")]
    assert "Nearly ready" in ada and "Tesco Extra" not in ada
    assert not [word for word in ENGINEERS_WORDS if word in ada], ada
    assert "Get Contents of URL" in ola and "Copy iCloud Link" in ola and "PRESENCE_SHORTCUT_URL" in ola


async def test_a_link_that_is_not_in_use_opens_a_page_that_says_so(client):
    async with tx() as conn:
        home, tokens = await household(conn)
        await members.new_presence_token(conn, home.members["Ola"])           # Ola's link is replaced
    for opened in (f"/presence/{tokens['Ola']}", "/presence/never-issued/enter/Tesco%20Extra"):
        page = await client.get(opened)
        assert page.status_code == 404 and "This link has stopped working" in page.text
        assert "send the word shops" in words_of(page) and "Tesco Extra" not in page.text
        assert page.headers["cache-control"] == "no-store"


async def test_a_link_copied_for_a_shop_is_the_whole_ping_with_no_body(client):
    async with tx() as conn:
        home, tokens = await household(conn)
        await the_usual_list(conn, home)
    for path in (f"/presence/{tokens['Ola']}/arrive/Tesco%20Extra", "/presence/wrong/enter/Tesco%20Extra",
                 f"/presence/{tokens['Ola']}/enter/{'x' * 81}"):
        assert (await client.post(path)).status_code == 204                 # the same answer, and nothing done
    assert await pings() == [] and await nudges() == []

    response = await client.post(f"/presence/{tokens['Ola']}/enter/Tesco%20Extra")
    assert (response.status_code, response.content) == (204, b"")
    await client.post(f"/presence/{tokens['Ola']}/exit/Home")
    assert await pings() == [("Ola", "Tesco Extra", "enter"), ("Ola", "Home", "exit")]
    assert await nudges() == [("Ola", TESCO_LIST)]
    await client.post(f"/presence/{tokens['Ola']}/enter/Fruit%2FVeg%20stall")      # a name with a slash in it
    assert (await pings())[-1] == ("Ola", "Fruit/Veg stall", "enter")


async def test_the_place_is_matched_whatever_its_case_or_spacing(client):
    async with tx() as conn:
        home, tokens = await household(conn)
        await the_usual_list(conn, home)
    await client.post(f"/presence/{tokens['Ola']}", json={"event": " Enter ", "place": "  tesco   EXTRA "})
    assert await pings() == [("Ola", "Tesco Extra", "enter")] and await nudges() == [("Ola", TESCO_LIST)]


async def test_an_unknown_place_is_remembered_as_other_and_home_as_home(client):
    async with tx() as conn:
        home, tokens = await household(conn)
        await the_usual_list(conn, home)
        await execute(conn, "delete from places where name = 'Home'")
        stranger, theirs = await household(conn, ola="2001", ada="2002")
        await execute(conn, "delete from places where household_id = :h", h=stranger.id)

    await ping(client, tokens["Ola"], "enter", "Corner shop")
    await ping(client, tokens["Ola"], "enter", "Corner shop")
    await ping(client, tokens["Ola"], "exit", "home")
    assert {(r["name"], r["kind"]) for r in await rows("select name, kind from places where household_id = :h",
                                                       h=home.id)} == {
        ("Tesco Extra", "store"), ("African shop on Rye Lane", "store"), ("Corner shop", "other"), ("home", "home")}
    assert await nudges() == []                # not known to be a shop, so no list

    # Another household's phone naming our shop makes a place of its own, and reaches nothing of ours.
    await ping(client, theirs["Ola"], "enter", "Tesco Extra")
    assert await rows("select name, kind from places where household_id = :h", h=stranger.id) == [
        {"name": "Tesco Extra", "kind": "other"}]
    assert await nudges() == []


async def test_a_token_is_good_for_thirty_pings_an_hour(client, clock):
    async with tx() as conn:
        home, tokens = await household(conn)
    clock("2026-10-06 11:00")
    await ping(client, tokens["Ola"], "enter", "Home")
    clock("2026-10-06 11:30")
    for _ in range(31):
        await ping(client, tokens["Ola"], "enter", "Home")
    assert len(await pings()) == 30
    await ping(client, tokens["Ada"], "enter", "Home")                       # the limit is per token
    assert len(await pings()) == 31
    async with tx() as conn:
        await the_usual_list(conn, home)
    await ping(client, tokens["Ola"], "enter", "Tesco Extra")                # dropped: no row, no list
    assert await nudges() == []

    clock("2026-10-06 12:00")                  # an hour after the first one: room for exactly one more
    await ping(client, tokens["Ola"], "enter", "Tesco Extra")
    await ping(client, tokens["Ola"], "enter", "Home")
    assert len(await pings()) == 32 and await nudges() == [("Ola", TESCO_LIST)]


async def test_a_failure_while_handling_a_ping_is_still_answered_204(client, monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("rules blew up")

    async with tx() as conn:
        _, tokens = await household(conn)
    monkeypatch.setattr(routes.rules, "apply", broken)
    await ping(client, tokens["Ola"], "enter", "Tesco Extra")
    assert await pings() == []                 # and the half-done work was rolled back


# ---------------------------------------------------------------- out and about
async def test_leaving_home_with_a_long_list_or_a_fresh_entry_offers_the_list_once_a_day(client, clock):
    async with tx() as conn:
        home, tokens = await household(conn)
        for n in range(7):
            await on_list(conn, home, f"thing {n}")
        await on_list(conn, home, "milk", reason="predicted")                # a guess does not make the list long
        await execute(conn, "update members set quiet_start = '21:30', quiet_end = '07:00'")

    clock("2026-10-06 06:30")                  # seven things, none of them new: not worth a message
    await ping(client, tokens["Ola"], "exit", "Home")
    assert await nudges() == []

    async with tx() as conn:
        await on_list(conn, home, "eggs")
    await ping(client, tokens["Ola"], "exit", "Home")
    await ping(client, tokens["Ola"], "exit", "Home")
    clock("2026-10-06 18:00")
    await ping(client, tokens["Ola"], "exit", "Home")                        # out again in the evening
    assert await nudges() == [("Ola", "You're out. The list has 8 items, want it?")]
    # 06:30 is inside Ola's quiet hours; someone walking out of the door is awake, so it is not held.
    assert await rows("select respect_quiet_hours from outbox") == [{"respect_quiet_hours": False}]

    await ping(client, tokens["Ada"], "exit", "Home")                        # once a day each
    clock("2026-10-07 08:00")
    await ping(client, tokens["Ola"], "exit", "Home")                        # and again the next day
    assert [who for who, _ in await nudges()] == ["Ola", "Ada", "Ola"]


async def test_a_short_list_is_offered_only_while_something_on_it_is_under_two_hours_old(client, clock):
    async with tx() as conn:
        home, tokens = await household(conn)
        await on_list(conn, home, "eggs", added_at=london("2026-10-06 09:00"))
        await on_list(conn, home, "milk", reason="predicted", added_at=london("2026-10-06 11:30"))

    clock("2026-10-06 11:00")                  # exactly two hours on: no longer fresh
    await ping(client, tokens["Ola"], "exit", "Home")
    await ping(client, tokens["Ola"], "enter", "Home")                       # coming home is never an offer
    await ping(client, tokens["Ola"], "exit", "Tesco Extra")                 # nor is leaving a shop
    assert await nudges() == []
    clock("2026-10-06 10:59")
    await ping(client, tokens["Ada"], "exit", "Home")
    assert await nudges() == [("Ada", "You're out. The list has 1 item, want it?")]


# ---------------------------------------------------------------- both home
async def a_shopping_trip(client, clock, tokens, *, to_the_shop=True):
    """Both leave with nothing worth offering; Ola shops (or not); both come home, Ada last."""
    clock("2026-10-06 09:00")
    await ping(client, tokens["Ada"], "exit", "Home")
    await ping(client, tokens["Ola"], "exit", "Home")
    clock("2026-10-06 10:00")
    await ping(client, tokens["Ola"], "enter", "Tesco Extra" if to_the_shop else "Library")
    clock("2026-10-06 11:00")
    await ping(client, tokens["Ola"], "enter", "Home")


async def test_once_everyone_is_home_after_a_shop_leaving_again_that_day_brings_no_offer(client, clock):
    async with tx() as conn:
        home, tokens = await household(conn)
        await on_list(conn, home, "eggs")
    await a_shopping_trip(client, clock, tokens)
    assert [who for who, _ in await nudges()] == ["Ola"]                     # the list at the shop

    # Ola is home but Ada is not, so the day is not settled yet.
    assert await rows("select 1 from nudge_log where dedupe_key like 'out:%'") == []
    clock("2026-10-06 12:00")
    await ping(client, tokens["Ada"], "enter", "Home")
    day = "2026-10-06"
    assert sorted(r["dedupe_key"] for r in await rows("select dedupe_key from nudge_log where dedupe_key like 'out:%'")) \
        == sorted(f"out:{home.members[who]}:{day}" for who in ("Ola", "Ada"))

    async with tx() as conn:
        await on_list(conn, home, "bread", added_at=london("2026-10-06 12:30"))
    clock("2026-10-06 13:00")
    await ping(client, tokens["Ola"], "exit", "Home")
    await ping(client, tokens["Ada"], "exit", "Home")
    assert [who for who, _ in await nudges()] == ["Ola"]                     # still only the list at the shop

    clock("2026-10-07 08:00")                  # tomorrow is a new day
    async with tx() as conn:
        await execute(conn, "update shopping_list_items set added_at = :at", at=london("2026-10-07 07:30"))
    await ping(client, tokens["Ola"], "exit", "Home")
    assert [who for who, _ in await nudges()] == ["Ola", "Ola"]


@pytest.mark.parametrize("to_the_shop,ada_comes_home", [(False, True), (True, False)])
async def test_without_a_shop_visit_or_with_someone_still_out_the_offer_still_comes(client, clock, to_the_shop,
                                                                                    ada_comes_home):
    async with tx() as conn:
        home, tokens = await household(conn)
        await on_list(conn, home, "eggs")
    await a_shopping_trip(client, clock, tokens, to_the_shop=to_the_shop)
    if ada_comes_home:
        clock("2026-10-06 12:00")
        await ping(client, tokens["Ada"], "enter", "Home")
    async with tx() as conn:
        await on_list(conn, home, "bread", added_at=london("2026-10-06 12:30"))
    clock("2026-10-06 13:00")
    await ping(client, tokens["Ola"], "exit", "Home")
    assert (await nudges())[-1] == ("Ola", "You're out. The list has 2 items, want it?")


async def test_an_adult_whose_phone_never_reports_does_not_keep_the_household_out(client, clock):
    async with tx() as conn:
        home, tokens = await household(conn)
        await add_member(conn, home, "Grace", telegram_id="1003")            # no Shortcuts on her phone
        await on_list(conn, home, "eggs")
    await a_shopping_trip(client, clock, tokens)
    await ping(client, tokens["Ada"], "enter", "Home")
    assert await fetch_count("select count(*) from nudge_log where dedupe_key like 'out:%'") == 3


async def fetch_count(sql):
    async with tx() as conn:
        return await fetch_val(conn, sql)


# ---------------------------------------------------------------- tokens stay out of the logs
@pytest.mark.parametrize("path,logged", [
    ("/presence/q8Zk3v_Lr-1x", "/presence/…"),
    ("/login/q8Zk3v_Lr-1x", "/login/…"),
    ("/ics/q8Zk3v_Lr-1x.ics", "/ics/…"),
    ("/setup?token=hunter2", "/setup?token=…"),
    ("/webhooks/imessage?x=1&secret=hunter2&y=2", "/webhooks/imessage?x=1&secret=…&y=2"),
    ("/dashboard/settings/presence/4e2c", "/dashboard/settings/presence/4e2c"),      # a member id, not a token
    ("/dashboard/shopping", "/dashboard/shopping"),
])
def test_the_access_log_never_shows_a_token_from_a_request_path(path, logged):
    import logging

    from app.config import _MaskAccessLog

    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("127.0.0.1:50000", "POST", path, "1.1", 204), None)
    assert _MaskAccessLog().filter(record) is True
    assert record.getMessage() == f'127.0.0.1:50000 - "POST {logged} HTTP/1.1" 204'
