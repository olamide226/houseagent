"""Dashboard: first-run setup, magic-link login, sessions, CSRF, and writes through the shared services."""
import json
import re
from datetime import time, timedelta

import respx

from app.agent.loop import LoopRuntime
from app.channels.base import ADAPTERS
from app.core.envelope import Channel, OutboundMessage
from app.core.timeutil import local, utcnow
from app.db import execute, fetch_all, fetch_one, fetch_val, tx
from app.main import app
from app.pipeline import router
from app.services import households, members
from app.worker import jobs
from tests.helpers import (
    FakeAdapter,
    FakeLLM,
    active_list,
    add_item,
    add_member,
    call,
    link,
    london,
    post_whatsapp,
    say,
    seed_home,
    stock_of,
    tg_update,
    wa_message,
    wa_webhook,
)

SETUP = "/setup?token=test-setup-token"
FORM = {"token": "test-setup-token", "household": "Adebayo", "timezone": "Europe/London", "admin": "Ola"}


async def login(client, home, member=None):
    """Log in through a real magic link. Returns the CSRF header for POSTs."""
    async with tx() as conn:
        token = await members.create_login_token(conn, member or home.ola, utcnow())
    response = await client.get(f"/login/{token}")
    assert response.status_code == 303 and response.headers["location"] == "/dashboard"
    page = await client.get("/dashboard")
    return {"X-CSRF-Token": re.search(r'"X-CSRF-Token": "([0-9a-f]+)"', page.text).group(1), "HX-Request": "true"}


# ---------------------------------------------------------------- setup
async def test_setup_needs_the_token_creates_the_household_once_then_disappears(client):
    assert (await client.get("/setup")).status_code == 404
    assert (await client.get("/setup?token=wrong")).status_code == 404
    assert (await client.get(SETUP)).status_code == 200
    assert (await client.post("/setup", data={**FORM, "token": "wrong"})).status_code == 404
    assert (await client.post("/setup", data={**FORM, "timezone": "Mars/Olympus"})).status_code == 422

    done = await client.post("/setup", data=FORM)
    assert done.status_code == 200
    code = re.search(r"<code>([A-Z]{4}-[A-Z0-9]{4})</code>", done.text).group(1)
    assert f"https://t.me/home_test_bot?start={code}" in done.text and "<svg" in done.text

    async with tx() as conn:
        admin = await fetch_one(conn, "select m.name, m.role, m.is_admin, h.name as household, h.timezone "
                                      "from members m join households h on h.id = m.household_id")
        locations = await fetch_all(conn, "select name from locations order by name")
    assert admin == {"name": "Ola", "role": "adult", "is_admin": True, "household": "Adebayo",
                     "timezone": "Europe/London"}
    assert [row["name"] for row in locations] == ["freezer", "fridge", "store"]

    assert (await client.get(SETUP)).status_code == 404
    assert (await client.post("/setup", data=FORM)).status_code == 404

    # The invite shown on the page really connects the admin's chat.
    await client.post("/webhooks/telegram", json=tg_update(1, f"/start {code}"),
                      headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"})
    async with tx() as conn:
        assert await fetch_one(conn, "select handle from channel_identities") == {"handle": "1001"}


# ---------------------------------------------------------------- login and sessions
async def test_pages_need_a_session_and_a_magic_link_works_exactly_once(client):
    async with tx() as conn:
        home = await seed_home(conn)
        token = await members.create_login_token(conn, home.ola, utcnow())
    for path in ("/dashboard", "/dashboard/shopping", "/dashboard/inventory", "/dashboard/calendar",
                 "/dashboard/activity", "/dashboard/playground", "/dashboard/family", "/dashboard/settings",
                 "/dashboard/channels"):
        refused = await client.get(path)
        assert refused.status_code == 401 and "login link" in refused.text

    first = await client.get(f"/login/{token}")
    assert first.status_code == 303
    cookie = first.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=2592000" in cookie
    assert (await client.get("/dashboard")).status_code == 200

    client.cookies.clear()
    assert (await client.get(f"/login/{token}")).status_code == 401       # single use
    assert (await client.get("/login/made-up")).status_code == 401
    assert (await client.get("/dashboard")).status_code == 401


async def test_a_tampered_cookie_is_refused_and_logout_everywhere_ends_other_sessions(client):
    async with tx() as conn:
        home = await seed_home(conn)
    csrf = await login(client, home)
    session = client.cookies["ha_session"]

    client.cookies.set("ha_session", session[:-2] + ("aa" if not session.endswith("aa") else "bb"))
    assert (await client.get("/dashboard")).status_code == 401

    client.cookies.set("ha_session", session)
    out = await client.post("/logout?everywhere=1", headers=csrf)
    assert out.status_code == 303
    client.cookies.set("ha_session", session)                             # a copy held by another device
    assert (await client.get("/dashboard")).status_code == 401


async def test_every_post_needs_the_csrf_token(client):
    async with tx() as conn:
        home = await seed_home(conn)
    csrf = await login(client, home)
    nothing = "00000000-0000-0000-0000-000000000000"
    posts = ["/dashboard/shopping/add", "/dashboard/playground", "/logout",
             f"/dashboard/activity/actions/{nothing}/undo", "/dashboard/calendar/add", "/dashboard/calendar/feed",
             f"/dashboard/calendar/events/{nothing}/cancel", f"/dashboard/calendar/reminders/{nothing}/cancel",
             "/dashboard/family/add", f"/dashboard/family/{home.ola}/invite", "/dashboard/settings/brief",
             f"/dashboard/settings/quiet/{home.ola}", "/dashboard/settings/facts",
             f"/dashboard/settings/presence/{home.ola}", "/dashboard/settings/places",
             "/dashboard/channels/whatsapp/group", f"/dashboard/channels/threads/{nothing}/primary",
             f"/dashboard/channels/threads/{nothing}/invite", f"/dashboard/channels/threads/{nothing}/forget"]
    for path in posts:
        form = {"item": "eggs", "text": "hi", "title": "GP", "when": "2030-01-01T10:00", "name": "Ada",
                "at": "05:00", "start": "20:00", "end": "08:00", "key": "milk", "value": "Arla",
                "subject": "Adebayo family", "kind": "store"}
        assert (await client.post(path, data=form)).status_code == 403
        assert (await client.post(path, data=form, headers={"X-CSRF-Token": "0" * 64})).status_code == 403
    async with tx() as conn:
        assert await active_list(conn, home) == {}
        assert await fetch_all(conn, "select 1 from events") == []
        assert await fetch_one(conn, "select calendar_token_hash, digest_time from households") == {
            "calendar_token_hash": None, "digest_time": time(7, 30)}
        assert await fetch_all(conn, "select name, invite_code_hash, quiet_start, presence_token_hash from members") == [
            {"name": "Ola", "invite_code_hash": None, "quiet_start": time(21, 30), "presence_token_hash": None}]
        assert await fetch_all(conn, "select 1 from household_facts") == []
        assert await fetch_all(conn, "select 1 from places") == []
        assert await fetch_all(conn, "select 1 from threads") == []
    # The token also works as a form field, for plain form posts.
    added = await client.post("/dashboard/shopping/add", data={"item": "eggs", "csrf": csrf["X-CSRF-Token"]})
    assert added.status_code == 303
    async with tx() as conn:
        assert await active_list(conn, home) == {"egg": "explicit"}


# ---------------------------------------------------------------- pages and writes
async def test_today_shows_list_count_low_and_expiring_items(client):
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "rice", status="low")
        await add_item(conn, home, "bread", qty=1)
    csrf = await login(client, home)
    await client.post("/dashboard/shopping/add", data={"item": "eggs"}, headers=csrf)
    page = (await client.get("/dashboard")).text
    assert ">1</a>" in page and "rice" in page and "bread" not in page


async def test_shopping_page_writes_go_through_the_services_and_are_logged_as_dashboard(client):
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "egg", location="fridge")
    csrf = await login(client, home)

    page = await client.post("/dashboard/shopping/add", data={"item": "eggs", "store_hint": "Tesco"}, headers=csrf)
    assert page.status_code == 200 and "egg" in page.text and "Tesco" in page.text
    await client.post("/dashboard/shopping/add", data={"item": "bleach"}, headers=csrf)
    async with tx() as conn:
        entries = {r["item"]: r["id"] for r in await fetch_all(
            conn, "select s.id, i.canonical_name as item from shopping_list_items s join items i on i.id = s.item_id")}

    await client.post(f"/dashboard/shopping/{entries['egg']}/bought", headers=csrf)
    await client.post(f"/dashboard/shopping/{entries['bleach']}/store", data={"store_hint": "Costco"}, headers=csrf)
    async with tx() as conn:
        # Ticking off in the browser restocked inventory, exactly as "bought eggs" in chat would.
        assert await stock_of(conn, home) == {("egg", "fridge"): (None, "in_stock")}
        assert await active_list(conn, home) == {"bleach": "explicit"}
        hint = await fetch_one(conn, "select store_hint from shopping_list_items where id = :id", id=entries["bleach"])
        logged = await fetch_all(conn, "select source, tool, member_id from agent_actions order by created_at")
    assert hint == {"store_hint": "Costco"}
    assert [(a["source"], a["tool"]) for a in logged] == [
        ("dashboard", "shopping.add"), ("dashboard", "shopping.add"),
        ("dashboard", "shopping.bought"), ("dashboard", "shopping.store")]
    assert {a["member_id"] for a in logged} == {home.ola}

    await client.post(f"/dashboard/shopping/{entries['bleach']}/remove", headers=csrf)
    gone = await client.post(f"/dashboard/shopping/{entries['bleach']}/remove", headers=csrf)
    assert "no longer on the list" in gone.text


async def test_inventory_page_filters_adjusts_edits_and_merges(client):
    async with tx() as conn:
        home = await seed_home(conn)
        rice = await add_item(conn, home, "rice", qty=5)
        dup = await add_item(conn, home, "basmati", qty=2)
        await add_item(conn, home, "milk", location="fridge", status="low")
        store = (await fetch_one(conn, "select id from locations where name = 'store' and household_id = :h", h=home.id))["id"]
        fridge = (await fetch_one(conn, "select id from locations where name = 'fridge' and household_id = :h", h=home.id))["id"]
    csrf = await login(client, home)

    everything = (await client.get("/dashboard/inventory")).text
    assert all(name in everything for name in ("rice", "basmati", "milk"))
    low = (await client.get("/dashboard/inventory?status=low", headers={"HX-Request": "true"})).text
    assert "milk" in low and "rice" not in low and "<html" not in low

    base = f"/dashboard/inventory/items/{rice}"
    await client.post(f"{base}/stock", data={"action": "adjusted", "location_id": store, "quantity": "3"}, headers=csrf)
    bad = await client.post(f"{base}/stock", data={"action": "adjusted", "location_id": store, "quantity": "lots"}, headers=csrf)
    assert "not a number" in bad.text
    await client.post(f"{base}/edit", data={"aliases": "Long grain, uncle bens", "is_staple": "true",
                                           "low_threshold": "1", "default_location_id": fridge}, headers=csrf)
    async with tx() as conn:
        assert (await stock_of(conn, home))[("rice", "store")][0] == 3
        item = await fetch_one(conn, "select aliases, is_staple, low_threshold, default_location_id from items where id = :id", id=rice)
    assert item == {"aliases": ["long grain", "uncle bens"], "is_staple": True, "low_threshold": 1,
                    "default_location_id": fridge}

    merged = await client.post(f"{base}/merge", data={"duplicate_id": dup}, headers=csrf)
    assert merged.status_code == 200
    async with tx() as conn:
        assert await fetch_one(conn, "select 1 from items where id = :id", id=dup) is None
        assert (await stock_of(conn, home))[("rice", "store")][0] == 5          # 3 + 2
        aliases = (await fetch_one(conn, "select aliases from items where id = :id", id=rice))["aliases"]
    assert "basmati" in aliases

    await client.post(f"{base}/stock", data={"action": "finished", "location_id": store}, headers=csrf)
    async with tx() as conn:
        assert await active_list(conn, home) == {"rice": "finished"}             # a staple, via the same rule
    detail = (await client.get(base)).text
    assert "finished" in detail and "adjusted" in detail


async def test_another_households_rows_cannot_be_read_or_changed(client):
    async with tx() as conn:
        home = await seed_home(conn)
        other = await seed_home(conn, telegram_id=None)
        theirs = await add_item(conn, other, "caviar", qty=1)
        store = (await fetch_one(conn, "select id from locations where name = 'store' and household_id = :h", h=other.id))["id"]
    csrf = await login(client, home)
    assert (await client.get(f"/dashboard/inventory/items/{theirs}")).status_code == 404
    await client.post(f"/dashboard/inventory/items/{theirs}/stock",
                      data={"action": "finished", "location_id": store}, headers=csrf)
    async with tx() as conn:
        assert (await stock_of(conn, other))[("caviar", "store")][1] == "in_stock"


async def test_activity_shows_turns_and_dashboard_actions_and_can_undo(client):
    async with tx() as conn:
        home = await seed_home(conn)
        await add_member(conn, home, "Ada", telegram_id="1002")
        await add_item(conn, home, "rice", staple=True, qty=3)
    await client.post("/webhooks/telegram", json=tg_update(1, "finished the rice"),
                      headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"})
    from app.pipeline import inbound
    await inbound.process_household(home.id, LoopRuntime(FakeLLM(
        call("log_inventory", changes=[{"item": "rice", "action": "finished"}]), say("ACK"))), {})

    csrf = await login(client, home, home.members["Ada"])                    # any adult can look and fix
    await client.post("/dashboard/shopping/add", data={"item": "bleach"}, headers=csrf)
    page = (await client.get("/dashboard/activity")).text
    assert "finished the rice" in page and "log_inventory" in page and "OK: rice finished" in page
    assert "200 in" in page and "shopping.add" in page and "pending" in page

    async with tx() as conn:
        action = await fetch_one(conn, "select id from agent_actions where tool = 'log_inventory'")
    undone = await client.post(f"/dashboard/activity/actions/{action['id']}/undo", headers=csrf)
    assert undone.status_code == 200 and "was undone" in undone.text
    async with tx() as conn:
        assert (await stock_of(conn, home))[("rice", "store")] == (3, "in_stock")
        assert await active_list(conn, home) == {"bleach": "explicit"}
    again = await client.post(f"/dashboard/activity/actions/{action['id']}/undo", headers=csrf)
    assert "already undone" in again.text


async def test_playground_is_a_dry_run_unless_apply_is_ticked(client):
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "rice", staple=True, qty=3)
    csrf = await login(client, home)
    script = [call("log_inventory", changes=[{"item": "rice", "action": "finished"}]), say("ACK")]

    app.state.runtime = LoopRuntime(FakeLLM(*script))
    dry = await client.post("/dashboard/playground", data={"text": "finished the rice"}, headers=csrf)
    assert "OK: rice finished" in dry.text and "dry run, nothing saved" in dry.text
    async with tx() as conn:
        assert (await stock_of(conn, home))[("rice", "store")] == (3, "in_stock")
        for table in ("messages", "outbox", "agent_actions", "inventory_events", "threads"):
            assert await fetch_all(conn, f"select 1 from {table}") == []

    app.state.runtime = LoopRuntime(FakeLLM(*script))
    real = await client.post("/dashboard/playground", data={"text": "finished the rice", "apply": "true"}, headers=csrf)
    assert "applied" in real.text
    async with tx() as conn:
        assert (await stock_of(conn, home))[("rice", "store")] == (0, "out")
        assert await active_list(conn, home) == {"rice": "finished"}

    app.state.runtime = LoopRuntime(FakeLLM())                               # the model call fails
    failed = await client.post("/dashboard/playground", data={"text": "hello", "apply": "true"}, headers=csrf)
    assert failed.status_code == 200 and "ran out of scripted responses" in failed.text


# ---------------------------------------------------------------- calendar
def in_days(days, clock="10:30"):
    """A datetime-local form value a few days from now, in household time."""
    return f"{local(utcnow() + timedelta(days=days), 'Europe/London'):%Y-%m-%d}T{clock}"


async def calendar_rows(home):
    async with tx() as conn:
        events = await fetch_all(conn, "select * from events where household_id = :h order by created_at", h=home.id)
        reminders = await fetch_all(
            conn, "select status, text from reminders where household_id = :h order by fire_at", h=home.id)
    return events, reminders


async def test_calendar_page_adds_edits_and_cancels_through_the_calendar_service(client):
    async with tx() as conn:
        home = await seed_home(conn)
        tobi = await add_member(conn, home, "Tobi", role="child")
    csrf = await login(client, home)
    empty = await client.get("/dashboard/calendar")
    assert empty.status_code == 200 and "Nothing booked" in empty.text

    page = await client.post("/dashboard/calendar/add", headers=csrf, data={
        "title": "Dentist", "when": in_days(3), "who": "Tobi", "location": "High St"})
    assert page.status_code == 200 and "Dentist" in page.text and "<html" not in page.text
    (event,), reminders = await calendar_rows(home)
    assert (event["title"], event["kind"], event["rrule"], event["location"]) == ("Dentist", "appointment", None, "High St")
    assert [str(p) for p in event["participant_ids"]] == [tobi] and event["created_by"] == home.ola
    assert f"{local(event['starts_at'], 'Europe/London'):%Y-%m-%dT%H:%M}" == in_days(3)
    # The same reminder plan a chat booking gets: the day before and an hour before, to the household.
    assert [r["status"] for r in reminders] == ["scheduled", "scheduled"]
    assert all(r["text"].startswith("Dentist for Tobi") for r in reminders)

    edited = await client.post(f"/dashboard/calendar/events/{event['id']}/edit", headers=csrf, data={
        "title": "Orthodontist", "when": in_days(4, "14:00"), "location": ""})
    assert "Orthodontist" in edited.text
    (event,), reminders = await calendar_rows(home)
    assert (event["title"], event["location"]) == ("Orthodontist", None)             # an empty field clears it
    assert f"{local(event['starts_at'], 'Europe/London'):%Y-%m-%dT%H:%M}" == in_days(4, "14:00")
    assert all("at 14:00" in r["text"] for r in reminders) and len(reminders) == 2

    await client.post(f"/dashboard/calendar/events/{event['id']}/cancel", headers=csrf)
    (event,), reminders = await calendar_rows(home)
    assert event["status"] == "cancelled" and {r["status"] for r in reminders} == {"cancelled"}
    async with tx() as conn:
        logged = await fetch_all(conn, "select source, tool, member_id from agent_actions order by created_at")
    assert [(a["source"], a["tool"], a["member_id"]) for a in logged] == [
        ("dashboard", "calendar.add", home.ola), ("dashboard", "calendar.edit", home.ola),
        ("dashboard", "calendar.cancel", home.ola)]

    for bad, why in [({"who": "Zed"}, "nobody called Zed"), ({"when": "soon"}, "pick a date and a time"),
                     ({"when": "2020-01-01T10:00"}, "already passed")]:
        refused = await client.post("/dashboard/calendar/add", headers=csrf, data={
            "title": "Nope", "when": in_days(3), **bad})
        assert why in refused.text
    assert len((await calendar_rows(home))[0]) == 1


async def test_calendar_page_skips_one_occurrence_of_a_series_and_activity_can_undo_it(client):
    async with tx() as conn:
        home = await seed_home(conn)
    csrf = await login(client, home)
    await client.post("/dashboard/calendar/add", headers=csrf, data={
        "title": "Chatterbox", "when": in_days(1, "09:00"), "repeat": "weekly"})
    (series,), _ = await calendar_rows(home)
    assert (series["rrule"], series["kind"]) == ("FREQ=WEEKLY", "activity")
    page = (await client.get("/dashboard/calendar")).text
    assert page.count("Skip this one") >= 4 and "Repeating" in page          # every week in the next 30 days

    second = local(series["starts_at"] + timedelta(days=7), "Europe/London").date()
    await client.post(f"/dashboard/calendar/events/{series['id']}/skip", headers=csrf, data={"day": second.isoformat()})
    (series,), _ = await calendar_rows(home)
    assert series["exdates"] == [second] and series["status"] == "active"
    assert f'value="{second.isoformat()}"' not in (await client.get("/dashboard/calendar")).text

    not_a_day = await client.post(f"/dashboard/calendar/events/{series['id']}/skip", headers=csrf, data={"day": "x"})
    assert "not a date" in not_a_day.text

    async with tx() as conn:
        action = await fetch_one(conn, "select id from agent_actions where tool = 'calendar.skip'")
    await client.post(f"/dashboard/activity/actions/{action['id']}/undo", headers=csrf)
    assert (await calendar_rows(home))[0][0]["exdates"] == []


async def test_calendar_page_lists_and_cancels_reminders_and_today_shows_what_is_coming(client):
    from app.agent.tools import run_tool
    from tests.helpers import ctx_for

    soon = utcnow() + timedelta(minutes=90)
    async with tx() as conn:
        home = await seed_home(conn)
        ctx = ctx_for(conn, home)
        for name, args in [
            ("set_reminder", {"text": "call the landlord", "fire_at": (soon + timedelta(minutes=5)).isoformat()}),
            ("set_reminder", {"text": "bins out", "rrule": "FREQ=DAILY;BYHOUR=18", "target": "household"}),
            ("schedule_event", {"title": "School run", "starts_at": soon.isoformat(), "location": "St Mary's"}),
            ("schedule_event", {"title": "Next week", "starts_at": (soon + timedelta(days=7)).isoformat()}),
        ]:
            result, is_error = await run_tool(name, args, ctx)
            assert not is_error, result
    csrf = await login(client, home)

    today = (await client.get("/dashboard")).text
    assert "School run" in today and "St Mary&#39;s" in today and "call the landlord" in today
    assert "Next week" not in today

    page = (await client.get("/dashboard/calendar")).text
    assert "call the landlord" in page and page.count("bins out") == 1 and "Next week" in page
    async with tx() as conn:
        reminder = await fetch_one(conn, "select id from reminders where text = 'call the landlord'")
    await client.post(f"/dashboard/calendar/reminders/{reminder['id']}/cancel", headers=csrf)
    again = await client.post(f"/dashboard/calendar/reminders/{reminder['id']}/cancel", headers=csrf)
    assert "no longer scheduled" in again.text
    async with tx() as conn:
        assert await fetch_one(conn, "select status from reminders where id = :id", id=reminder["id"]) == {
            "status": "cancelled"}


async def test_calendar_page_gives_a_subscribe_link_that_serves_the_feed(client):
    async with tx() as conn:
        home = await seed_home(conn)
    csrf = await login(client, home)
    assert "/ics/" not in (await client.get("/dashboard/calendar")).text
    page = await client.post("/dashboard/calendar/feed", headers=csrf)
    link = re.search(r'value="http://testserver(/ics/[\w-]+\.ics)"', page.text).group(1)
    assert (await client.get(link)).status_code == 200
    assert "/ics/" not in (await client.get("/dashboard/calendar")).text         # shown once only


async def test_another_households_calendar_cannot_be_changed(client):
    from app.agent.tools import run_tool
    from tests.helpers import ctx_for

    async with tx() as conn:
        home = await seed_home(conn)
        other = await seed_home(conn, telegram_id=None)
        ctx = ctx_for(conn, other)
        when = (utcnow() + timedelta(days=2)).isoformat()
        await run_tool("schedule_event", {"title": "Their GP", "starts_at": when}, ctx)
        await run_tool("set_reminder", {"text": "their reminder", "fire_at": when}, ctx)
        event = await fetch_one(conn, "select id from events")
        reminder = await fetch_one(conn, "select id from reminders where event_id is null")
    csrf = await login(client, home)
    assert "Their GP" not in (await client.get("/dashboard/calendar")).text
    for path in (f"events/{event['id']}/cancel", f"events/{event['id']}/edit", f"reminders/{reminder['id']}/cancel"):
        refused = await client.post(f"/dashboard/calendar/{path}", headers=csrf,
                                    data={"title": "Mine", "when": in_days(3)})
        assert "no longer" in refused.text
    (theirs,), reminders = await calendar_rows(other)
    assert (theirs["title"], theirs["status"]) == ("Their GP", "active")
    assert {r["status"] for r in reminders} == {"scheduled"}


async def test_health_endpoints(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/readyz")).json() == {"status": "ready"}


# ---------------------------------------------------------------- Family and Settings
SECRET = {"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"}


async def test_family_page_adds_an_adult_whose_invite_connects_them_without_anyone_touching_sql(client):
    async with tx() as conn:
        home = await seed_home(conn)
    csrf = await login(client, home)

    added = await client.post("/dashboard/family/add", data={"name": " Ada ", "role": "adult"}, headers=csrf)
    code = re.search(r"<code>([A-Z]{4}-[A-Z0-9]{4})</code>", added.text).group(1)
    assert f"https://t.me/home_test_bot?start={code}" in added.text and "<svg" in added.text
    assert code not in (await client.get("/dashboard/family")).text          # shown once: only its hash is kept
    again = await client.post("/dashboard/family/add", data={"name": "ada", "role": "adult"}, headers=csrf)
    assert "already in the family" in again.text
    await client.post("/dashboard/family/add", data={"name": "Tobi", "role": "child"}, headers=csrf)

    async with tx() as conn:
        family = await fetch_all(conn, "select name, role, invite_code_hash is not null as invited from members "
                                       "order by created_at")
    assert family == [{"name": "Ola", "role": "adult", "invited": False},
                      {"name": "Ada", "role": "adult", "invited": True},
                      {"name": "Tobi", "role": "child", "invited": False}]

    # Ada taps the link on her own phone and is in.
    await client.post("/webhooks/telegram", json=tg_update(1, f"/start {code}", user_id=1002, name="Ada"),
                      headers=SECRET)
    page = (await client.get("/dashboard/family")).text
    async with tx() as conn:
        linked = await fetch_one(conn, "select m.name, m.preferred_channel from channel_identities ci "
                                       "join members m on m.id = ci.member_id where ci.handle = '1002'")
        logged = await fetch_all(conn, "select source, tool, member_id, inverse <> '[]' as undoable "
                                       "from agent_actions order by created_at")
    assert linked == {"name": "Ada", "preferred_channel": "telegram"}
    assert page.count("telegram") == 2 and "not connected" not in page       # both adults show their channel
    activity = (await client.get("/dashboard/activity")).text
    assert "invite.redeem" in activity and "family.add" in activity          # connecting shows up beside the add
    assert [(a["source"], a["tool"], a["undoable"]) for a in logged] == [
        ("dashboard", "family.add", True), ("dashboard", "family.add", True), ("agent", "invite.redeem", False)]
    # And Ada, now an adult with a chat, can log in herself.
    async with tx() as conn:
        ada = (await fetch_one(conn, "select id from members where name = 'Ada'"))["id"]
    assert (await login(client, home, ada))["X-CSRF-Token"]


async def test_family_page_replaces_and_revokes_invites_and_sets_the_preferred_channel(client):
    async with tx() as conn:
        home = await seed_home(conn)
        ada = await add_member(conn, home, "Ada")
        tobi = await add_member(conn, home, "Tobi", role="child")
    csrf = await login(client, home)

    def code_in(page):
        return re.search(r"<code>([A-Z]{4}-[A-Z0-9]{4})</code>", page.text).group(1)

    first = code_in(await client.post(f"/dashboard/family/{ada}/invite", headers=csrf))
    second = code_in(await client.post(f"/dashboard/family/{ada}/invite", headers=csrf))
    assert "invite open until" in (await client.get("/dashboard/family")).text
    await client.post("/webhooks/telegram", json=tg_update(1, first, user_id=1002, name="Ada"), headers=SECRET)
    async with tx() as conn:
        assert await fetch_all(conn, "select 1 from channel_identities where member_id = :m", m=ada) == []   # replaced

    revoked = await client.post(f"/dashboard/family/{ada}/revoke", headers=csrf)
    assert "invite open until" not in revoked.text
    await client.post("/webhooks/telegram", json=tg_update(2, second, user_id=1002, name="Ada"), headers=SECRET)
    async with tx() as conn:
        assert await fetch_all(conn, "select 1 from channel_identities where member_id = :m", m=ada) == []   # revoked

    assert "only adults" in (await client.post(f"/dashboard/family/{tobi}/invite", headers=csrf)).text
    refused = await client.post(f"/dashboard/family/{home.ola}/channel", data={"channel": "whatsapp"}, headers=csrf)
    assert "has not connected whatsapp" in refused.text
    async with tx() as conn:
        await execute(conn, "insert into channel_identities (member_id, channel, handle) "
                            "values (:m, 'whatsapp', '+447700900001')", m=home.ola)
    await client.post(f"/dashboard/family/{home.ola}/channel", data={"channel": "whatsapp"}, headers=csrf)
    async with tx() as conn:
        assert await fetch_one(conn, "select preferred_channel from members where id = :m", m=home.ola) == {
            "preferred_channel": "whatsapp"}


async def test_settings_page_changes_the_brief_time_quiet_hours_and_facts_through_the_services(client):
    async with tx() as conn:
        home = await seed_home(conn)
        ada = await add_member(conn, home, "Ada", telegram_id="1002")
    csrf = await login(client, home)
    page = (await client.get("/dashboard/settings")).text
    assert 'value="07:30"' in page and 'value="21:30"' in page

    await client.post("/dashboard/settings/brief", data={"at": "06:45"}, headers=csrf)
    await client.post(f"/dashboard/settings/quiet/{ada}", data={"start": "22:30", "end": "06:00"}, headers=csrf)
    await client.post(f"/dashboard/settings/quiet/{home.ola}", data={"start": "", "end": ""}, headers=csrf)
    await client.post("/dashboard/settings/facts", data={"key": "Milk brand", "value": "Cravendale"}, headers=csrf)
    await client.post("/dashboard/settings/facts", data={"key": "shops", "value": "Tesco Extra, Costco"}, headers=csrf)
    await client.post("/dashboard/settings/facts", data={"key": "allergy", "value": "peanuts", "member_id": ada},
                      headers=csrf)

    async def state():
        async with tx() as conn:
            return {
                "brief": (await fetch_one(conn, "select digest_time from households"))["digest_time"],
                "quiet": {r["name"]: (r["quiet_start"], r["quiet_end"]) for r in await fetch_all(
                    conn, "select name, quiet_start, quiet_end from members")},
                "facts": {(r["key"], r["member_id"]): r["value"] for r in await fetch_all(
                    conn, "select key, member_id, value from household_facts")},
                "shops": sorted(r["name"] for r in await fetch_all(conn, "select name from places where kind = 'store'")),
            }

    saved = await state()
    assert saved == {
        "brief": time(6, 45), "quiet": {"Ola": (None, None), "Ada": (time(22, 30), time(6))},
        "facts": {("milk_brand", None): "Cravendale", ("shops", None): "Tesco Extra, Costco", ("allergy", ada): "peanuts"},
        "shops": ["Costco", "Tesco Extra"]}
    # The quiet hours just saved are what holds a send: a 23:00 reminder for Ada waits until 06:00.
    async with tx() as conn:
        assert local(await members.quiet_until(conn, home.id, ada, london("2026-10-06 23:00")),
                     "Europe/London").strftime("%d %H:%M") == "07 06:00"
        assert await members.quiet_until(conn, home.id, home.ola, london("2026-10-06 23:00")) is None

    for path, form in ((f"/dashboard/settings/quiet/{ada}", {"start": "22:00", "end": ""}),
                       ("/dashboard/settings/brief", {"at": "breakfast"}),
                       ("/dashboard/settings/facts", {"key": "quiet_hours", "value": "22:00-07:00"})):
        assert 'class="error"' in (await client.post(path, data=form, headers=csrf)).text
    assert await state() == saved

    await client.post("/dashboard/settings/facts", data={"key": "milk_brand", "value": "Arla"}, headers=csrf)
    await client.post("/dashboard/settings/facts", data={"key": "allergy", "value": "", "member_id": ada}, headers=csrf)
    assert (await state())["facts"] == {("milk_brand", None): "Arla", ("shops", None): "Tesco Extra, Costco"}

    # Every change is in Activity as a dashboard action and can be undone from there.
    async with tx() as conn:
        logged = await fetch_all(conn, "select id, source, tool from agent_actions order by created_at")
    assert {a["source"] for a in logged} == {"dashboard"}
    assert [a["tool"] for a in logged] == ["settings.brief", "settings.quiet_hours", "settings.quiet_hours",
                                           "settings.fact", "settings.fact", "settings.fact", "settings.fact",
                                           "settings.fact"]
    for action in (logged[0], logged[1]):
        await client.post(f"/dashboard/activity/actions/{action['id']}/undo", headers=csrf)
    restored = await state()
    assert restored["brief"] == time(7, 30) and restored["quiet"]["Ada"] == (time(21, 30), time(7))


async def test_family_and_settings_cannot_reach_another_household(client):
    async with tx() as conn:
        home = await seed_home(conn)
        other_household, stranger = await households.create_household(conn, "Other", "Europe/London", "Sam")
    csrf = await login(client, home)
    for path, form in ((f"/dashboard/family/{stranger}/invite", {}),
                       (f"/dashboard/family/{stranger}/revoke", {}),
                       (f"/dashboard/settings/quiet/{stranger}", {"start": "10:00", "end": "11:00"}),
                       (f"/dashboard/settings/presence/{stranger}", {}),
                       ("/dashboard/settings/facts", {"key": "note", "value": "x", "member_id": stranger})):
        response = (await client.post(path, data=form, headers=csrf)).text
        assert 'class="error"' in response and "testserver/presence/" not in response
    page = (await client.get("/dashboard/family")).text + (await client.get("/dashboard/settings")).text
    assert "Sam" not in page
    async with tx() as conn:
        sam = await fetch_one(conn, "select invite_code_hash, quiet_start, presence_token_hash from members "
                                    "where id = :m", m=stranger)
        assert sam == {"invite_code_hash": None, "quiet_start": time(21, 30), "presence_token_hash": None}
        assert await fetch_all(conn, "select 1 from household_facts") == []


async def test_settings_makes_and_replaces_a_presence_link_that_is_shown_once_and_works(client):
    async with tx() as conn:
        home = await seed_home(conn)
        ada = await add_member(conn, home, "Ada", telegram_id="1002")
        tobi = await add_member(conn, home, "Tobi", role="child")
        await execute(conn, "insert into places (household_id, name, kind) values (:h, 'Tesco Extra', 'store')", h=home.id)
        await add_item(conn, home, "egg")
        await execute(conn, "insert into shopping_list_items (household_id, item_id, reason) "
                            "select household_id, id, 'explicit' from items")
    csrf = await login(client, home)
    link = re.compile(r'value="(http://testserver/presence/([\w-]{43}))"')
    page = (await client.get("/dashboard/settings")).text
    assert page.count("no link yet") == 2 and "Make link" in page and "testserver/presence/" not in page and "Tobi" not in \
        page.split("Arriving at the shops")[1].split("<h2>Places")[0]

    made = (await client.post(f"/dashboard/settings/presence/{ada}", headers=csrf)).text
    (url, token), = link.findall(made)
    assert "Ada's personal link" in made and "Get Contents of URL" in made and "Replace link" in made
    assert "testserver/presence/" not in (await client.get("/dashboard/settings")).text        # shown that once only
    async with tx() as conn:
        stored = await fetch_val(conn, "select presence_token_hash from members where id = :m", m=ada)
        (action,) = await fetch_all(conn, "select source, tool, args, result, inverse from agent_actions")
    assert stored and token not in stored and token not in json.dumps(action)
    assert (action["source"], action["tool"], action["inverse"]) == ("dashboard", "settings.presence", [])

    # The link is Ada's: her phone calling it at the shop queues the list for her.
    assert (await client.post(url.removeprefix("http://testserver"),
                              json={"event": "enter", "place": "Tesco Extra"})).status_code == 204
    async with tx() as conn:
        assert await fetch_all(conn, "select member_id, urgency from outbox") == [{"member_id": ada, "urgency": "high"}]

    # Replacing it stops the old link working at once.
    replaced = (await client.post(f"/dashboard/settings/presence/{ada}", headers=csrf)).text
    (new_url, _), = link.findall(replaced)
    async with tx() as conn:
        await execute(conn, "delete from nudge_log")
    await client.post(url.removeprefix("http://testserver"), json={"event": "enter", "place": "Tesco Extra"})
    async with tx() as conn:
        assert len(await fetch_all(conn, "select 1 from outbox")) == 1
    await client.post(new_url.removeprefix("http://testserver"), json={"event": "enter", "place": "Tesco Extra"})
    async with tx() as conn:
        assert len(await fetch_all(conn, "select 1 from outbox")) == 2

    refused = (await client.post(f"/dashboard/settings/presence/{tobi}", headers=csrf)).text
    assert 'class="error"' in refused and "testserver/presence/" not in refused
    async with tx() as conn:
        assert await fetch_val(conn, "select presence_token_hash from members where id = :m", m=tobi) is None


async def test_settings_adds_places_and_a_place_a_phone_named_sends_its_list_once_it_is_made_a_store(client):
    async with tx() as conn:
        home = await seed_home(conn)
        token = await members.new_presence_token(conn, home.ola)
        await add_item(conn, home, "egg")
        await execute(conn, "insert into shopping_list_items (household_id, item_id, reason) "
                            "select household_id, id, 'explicit' from items")
        other, _ = await households.create_household(conn, "Other", "Europe/London", "Sam")
        await execute(conn, "insert into places (household_id, name, kind) values (:h, 'Their gym', 'other')", h=other)
    csrf = await login(client, home)

    async def places():
        async with tx() as conn:
            return {r["name"]: r["kind"] for r in await fetch_all(
                conn, "select name, kind from places where household_id = :h", h=home.id)}

    arrive = {"event": "enter", "place": "Corner shop"}
    await client.post(f"/presence/{token}", json=arrive)                 # the phone names a place nobody added
    page = (await client.get("/dashboard/settings")).text
    assert await places() == {"Corner shop": "other"} and "Corner shop" in page and "Their gym" not in page
    async with tx() as conn:
        assert await fetch_all(conn, "select 1 from outbox") == []

    await client.post("/dashboard/settings/places", data={"name": "corner shop", "kind": "store"}, headers=csrf)
    await client.post("/dashboard/settings/places", data={"name": " Home ", "kind": "home"}, headers=csrf)
    await client.post("/dashboard/settings/places", data={"name": "Their gym", "kind": "clinic"}, headers=csrf)
    assert await places() == {"Corner shop": "store", "Home": "home", "Their gym": "clinic"}
    await client.post(f"/presence/{token}", json=arrive)
    async with tx() as conn:
        assert [r["text"] for r in await fetch_all(conn, "select text from outbox")] == [
            "You're at Corner shop. On the list:\n- egg"]
        assert await fetch_val(conn, "select kind from places where household_id = :h", h=other) == "other"

    for form in ({"name": "Lidl", "kind": "supermarket"}, {"name": "  ", "kind": "store"}):
        assert 'class="error"' in (await client.post("/dashboard/settings/places", data=form, headers=csrf)).text
    assert "Lidl" not in await places()

    # Logged as dashboard actions, and undo puts the kind back.
    async with tx() as conn:
        logged = await fetch_all(conn, "select id, source, tool, result from agent_actions order by created_at")
    assert [(a["source"], a["tool"]) for a in logged] == [("dashboard", "settings.place")] * 3
    assert logged[0]["result"] == "OK: corner shop is a place of kind store"
    await client.post(f"/dashboard/activity/actions/{logged[0]['id']}/undo", headers=csrf)
    assert (await places())["Corner shop"] == "other"


# ---------------------------------------------------------------- Channels
GRAPH = "https://graph.facebook.com/v26.0"
WA_GROUP = "Y2FwaV9ncm91cDo0NDc3MDA5MDAxMDA6MTIwMzYzMDAwMDAwMDAwMDAwZAZD"
OLA_WA = "GB.1000000000000000000101"


async def a_thread(conn, home, channel, external, scope="dm", heard=None):
    thread_id = await fetch_val(
        conn, "insert into threads (household_id, channel, external_thread_id, scope) values (:h, :c, :e, :s) "
              "returning id", h=home.id, c=channel, e=external, s=scope)
    if heard:
        await execute(conn, "insert into messages (household_id, thread_id, member_id, direction, text, created_at) "
                            "values (:h, :t, :m, 'in', 'hello', :at)", h=home.id, t=thread_id, m=home.ola, at=heard)
    return thread_id


def group_created(subject="Adebayo family", **group):
    return wa_webhook("group_lifecycle_update", groups=[{
        "timestamp": "1791230900", "group_id": WA_GROUP, "type": "group_create", "request_id": "r1",
        "subject": subject, "invite_link": "https://chat.whatsapp.com/EXAMPLEinviteLINK01", **group}])


async def test_channels_page_shows_each_channel_its_chats_and_what_whatsapp_will_deliver(client):
    now = utcnow()
    async with tx() as conn:
        home = await seed_home(conn)
        await link(conn, home.ola, OLA_WA, "whatsapp", verified_at=now - timedelta(days=40))
        ada = await add_member(conn, home, "Ada", whatsapp_id="GB.1000000000000000000102")
        await a_thread(conn, home, "telegram", "1001", heard=now - timedelta(hours=3))
        await a_thread(conn, home, "whatsapp", OLA_WA, heard=now - timedelta(days=2))        # window closed
        await a_thread(conn, home, "whatsapp", "GB.1000000000000000000102", heard=now - timedelta(hours=1))
        group = await a_thread(conn, home, "telegram", "-100555", scope="group")
        await execute(conn, "update households set primary_thread_id = :t where id = :h", t=group, h=home.id)
        await execute(conn, "insert into outbox (household_id, target, member_id, text, status, channel_used) "
                            "values (:h, 'member', :m, 'x', 'failed', 'whatsapp')", h=home.id, m=ada)
        other = await seed_home(conn, telegram_id="2001")
        await a_thread(conn, other, "telegram", "-100999", scope="group")
    await login(client, home)

    page = re.sub(r"\s+", " ", (await client.get("/dashboard/channels")).text)
    assert "<strong>telegram</strong> <span class=\"muted\">on, last heard from" in page
    assert "<strong>whatsapp</strong> <span class=\"muted\">on, last heard from" in page
    assert "1 failed send in the last day" in page
    assert page.count("Family group") == 1 and "main family chat" in page         # the other household's is not here
    assert "<strong>Ola</strong> <span class=\"muted\">telegram" in page
    assert page.count("template messages only until they next write") == 1          # Ola's WhatsApp DM
    assert page.count("ordinary messages until") == 1                               # Ada's
    assert "Create a whatsapp group" in page and "household_reminder" in page
    assert "<strong>imessage</strong> <span class=\"muted\">on, last heard from never" in page
    assert "not reachable since" not in page

    # The worker's five-minute check finds BlueBubbles down at 12:00; this household uses iMessage.
    async with tx() as conn:
        await link(conn, home.ola, "+447700900101", "imessage")
    with respx.mock:
        respx.get("http://mac-mini.test:1234/api/v1/ping").respond(502)
        await jobs.imessage_health(ADAPTERS[Channel.imessage], london("2026-10-06 12:00"))
    page = re.sub(r"\s+", " ", (await client.get("/dashboard/channels")).text)
    assert "not reachable since Tue 6 Oct 12:00; messages go to people's other channel" in page
    assert page.count("not reachable since") == 1                                   # said of iMessage only


async def test_a_chat_that_has_only_just_connected_counts_as_heard_from(client):
    async with tx() as conn:
        home = await seed_home(conn, telegram_id=None)
        await link(conn, home.ola, OLA_WA, "whatsapp")                          # connected a moment ago...
        await a_thread(conn, home, "whatsapp", OLA_WA)                          # ...and has not written since
    await login(client, home)
    page = (await client.get("/dashboard/channels")).text
    assert "ordinary messages until" in page and "template messages only" not in page
    assert page.count("last heard from never") == 2                             # Telegram and iMessage; not Ola's chat


async def test_channels_page_without_whatsapp_offers_no_group_and_refuses_the_request(client):
    async with tx() as conn:
        home = await seed_home(conn)
    csrf = await login(client, home)
    del ADAPTERS[Channel.whatsapp]
    page = await client.get("/dashboard/channels")
    assert "Create a whatsapp group" not in page.text and "not set up" in page.text
    for channel in ("whatsapp", "telegram", "carrier-pigeon"):
        refused = await client.post(f"/dashboard/channels/{channel}/group", data={"subject": "Family"}, headers=csrf)
        assert "cannot create groups" in refused.text
    async with tx() as conn:
        assert await fetch_all(conn, "select 1 from threads") == []


async def test_set_primary_thread_moves_household_sends_and_only_to_this_households_groups(client):
    async with tx() as conn:
        home = await seed_home(conn)
        dm = await a_thread(conn, home, "telegram", "1001")
        first = await a_thread(conn, home, "telegram", "-100555", scope="group")
        second = await a_thread(conn, home, "whatsapp", WA_GROUP, scope="group")
        await execute(conn, "update households set primary_thread_id = :t where id = :h", t=first, h=home.id)
        other = await seed_home(conn, telegram_id="2001")
        theirs = await a_thread(conn, other, "telegram", "-100999", scope="group")
    csrf = await login(client, home)

    async def primary():
        async with tx() as conn:
            return await fetch_val(conn, "select primary_thread_id from households where id = :h", h=home.id)

    for refused in (dm, theirs, "00000000-0000-0000-0000-000000000000"):
        page = await client.post(f"/dashboard/channels/threads/{refused}/primary", headers=csrf)
        assert "not one of this household" in page.text and await primary() == first

    page = await client.post(f"/dashboard/channels/threads/{second}/primary", headers=csrf)
    assert page.status_code == 200 and await primary() == second
    async with tx() as conn:
        await router.enqueue(conn, OutboundMessage(household_id=home.id, target="household", text="brief",
                                                   respect_quiet_hours=False))
        logged = await fetch_all(conn, "select source, tool, member_id from agent_actions")
    telegram, whatsapp = FakeAdapter(), FakeAdapter(Channel.whatsapp)
    await router.dispatch_due({Channel.telegram: telegram, Channel.whatsapp: whatsapp})
    assert whatsapp.sent == [(WA_GROUP, "brief", None)] and telegram.sent == []
    assert logged == [{"source": "dashboard", "tool": "channels.primary", "member_id": home.ola}]


@respx.mock
async def test_create_whatsapp_group_becomes_the_primary_thread_when_meta_confirms_and_shows_its_invite(client):
    create = respx.post(f"{GRAPH}/100000000000001/groups").respond(json={"messaging_product": "whatsapp"})
    async with tx() as conn:
        home = await seed_home(conn)
        old = await a_thread(conn, home, "telegram", "-100555", scope="group")
        await execute(conn, "update households set primary_thread_id = :t where id = :h", t=old, h=home.id)
    csrf = await login(client, home)

    async def state():
        async with tx() as conn:
            return await fetch_one(
                conn, "select t.external_thread_id, t.scope, t.id = h.primary_thread_id as is_primary "
                      "from threads t join households h on h.id = t.household_id where t.channel = 'whatsapp'")

    asked = await client.post("/dashboard/channels/whatsapp/group", data={"subject": " Adebayo  family "}, headers=csrf)
    assert "being created" in asked.text
    assert json.loads(create.calls.last.request.content) == {"messaging_product": "whatsapp", "subject": "Adebayo family"}
    assert await state() == {"external_thread_id": "pending:Adebayo family", "scope": "group", "is_primary": False}

    again = await client.post("/dashboard/channels/whatsapp/group", data={"subject": "Adebayo family"}, headers=csrf)
    assert "already being created" in again.text and create.call_count == 1
    waiting = await client.post(f"/dashboard/channels/threads/{await thread_id_of()}/invite", headers=csrf)
    assert "not one of this household" in waiting.text                          # no link until the group exists

    assert (await post_whatsapp(client, group_created(subject="Somebody else's group"))).status_code == 200
    assert (await state())["external_thread_id"] == "pending:Adebayo family"      # not the one we asked for
    assert (await post_whatsapp(client, group_created())).status_code == 200
    assert (await post_whatsapp(client, group_created())).status_code == 200      # Meta repeats itself
    assert await state() == {"external_thread_id": WA_GROUP, "scope": "group", "is_primary": True}

    link_route = respx.get(f"{GRAPH}/{WA_GROUP}/invite_link").respond(json={
        "messaging_product": "whatsapp", "invite_link": "https://chat.whatsapp.com/EXAMPLEinviteLINK01"})
    shown = await client.post(f"/dashboard/channels/threads/{await thread_id_of()}/invite", headers=csrf)
    assert 'href="https://chat.whatsapp.com/EXAMPLEinviteLINK01"' in shown.text and "<svg" in shown.text
    assert link_route.call_count == 1
    assert "EXAMPLEinviteLINK01" not in (await client.get("/dashboard/channels")).text   # never stored
    async with tx() as conn:
        logged = await fetch_all(conn, "select source, tool, result from agent_actions")
    assert logged == [{"source": "dashboard", "tool": "channels.group",
                       "result": "OK: asked whatsapp for a group called Adebayo family"}]

    # A household send now goes to the new group, through the real adapter.
    send = respx.post(f"{GRAPH}/100000000000001/messages").respond(json={"messages": [{"id": "wamid.sent0001"}]})
    async with tx() as conn:
        await router.enqueue(conn, OutboundMessage(household_id=home.id, target="household", text="Bins tonight",
                                                   respect_quiet_hours=False))
    await router.dispatch_due(ADAPTERS)
    sent = json.loads(send.calls.last.request.content)
    assert (sent["recipient_type"], sent["to"], sent["type"]) == ("group", WA_GROUP, "template")   # nobody has written there yet


async def thread_id_of(home=None):
    async with tx() as conn:
        return await fetch_val(conn, "select id from threads where channel = 'whatsapp' "
                                     "and (cast(:h as uuid) is null or household_id = :h)", h=home and home.id)


@respx.mock
async def test_a_group_meta_refuses_leaves_nothing_behind(client):
    async with tx() as conn:
        home = await seed_home(conn)
    csrf = await login(client, home)

    respx.post(f"{GRAPH}/100000000000001/groups").respond(400, json={"error": {
        "message": "Phone number is not eligible for groups", "type": "OAuthException", "code": 131215}})
    refused = await client.post("/dashboard/channels/whatsapp/group", data={"subject": "Adebayo family"}, headers=csrf)
    assert "131215 Phone number is not eligible for groups" in refused.text
    blank = await client.post("/dashboard/channels/whatsapp/group", data={"subject": "x" * 129}, headers=csrf)
    assert "up to 128 characters" in blank.text
    async with tx() as conn:
        assert await fetch_all(conn, "select 1 from threads") == []
        assert await fetch_all(conn, "select 1 from agent_actions") == []

    # Accepted, then refused later by webhook: the request is dropped and can be made again.
    respx.post(f"{GRAPH}/100000000000001/groups").respond(json={"messaging_product": "whatsapp"})
    await client.post("/dashboard/channels/whatsapp/group", data={"subject": "Adebayo family"}, headers=csrf)
    await post_whatsapp(client, group_created(errors=[{"code": 131215, "title": "Groups not eligible"}]))
    async with tx() as conn:
        assert await fetch_all(conn, "select 1 from threads") == []
        assert await fetch_val(conn, "select primary_thread_id from households where id = :h", h=home.id) is None
    retried = await client.post("/dashboard/channels/whatsapp/group", data={"subject": "Adebayo family"}, headers=csrf)
    assert "being created" in retried.text

    # Meta never answers: the request can be forgotten and made again, but only a pending one, and only ours.
    async with tx() as conn:
        real = await a_thread(conn, home, "telegram", "-100555", scope="group")
        other = await seed_home(conn, telegram_id="2001")
        theirs = await a_thread(conn, other, "whatsapp", "pending:Theirs", scope="group")
    for refused in (real, theirs):
        page = await client.post(f"/dashboard/channels/threads/{refused}/forget", headers=csrf)
        assert "not a group waiting to be created" in page.text
    forgotten = await client.post(f"/dashboard/channels/threads/{await thread_id_of(home)}/forget", headers=csrf)
    assert "being created" not in forgotten.text
    async with tx() as conn:
        left = await fetch_all(conn, "select external_thread_id from threads order by external_thread_id")
    assert [row["external_thread_id"] for row in left] == ["-100555", "pending:Theirs"]
    again = await client.post("/dashboard/channels/whatsapp/group", data={"subject": "Adebayo family"}, headers=csrf)
    assert "being created" in again.text


@respx.mock
async def test_a_group_whose_id_comes_back_at_once_or_was_already_written_in_is_the_primary_thread(client):
    async with tx() as conn:
        home = await seed_home(conn)
        await link(conn, home.ola, OLA_WA, "whatsapp")
    csrf = await login(client, home)

    async def groups():
        async with tx() as conn:
            return await fetch_all(
                conn, "select t.external_thread_id, t.id = h.primary_thread_id as is_primary "
                      "from threads t join households h on h.id = t.household_id where t.scope = 'group' "
                      "order by t.created_at")

    respx.post(f"{GRAPH}/100000000000001/groups").respond(json={"messaging_product": "whatsapp", "id": WA_GROUP})
    await client.post("/dashboard/channels/whatsapp/group", data={"subject": "Adebayo family"}, headers=csrf)
    assert await groups() == [{"external_thread_id": WA_GROUP, "is_primary": True}]
    await post_whatsapp(client, group_created())                                  # the webhook still comes
    assert await groups() == [{"external_thread_id": WA_GROUP, "is_primary": True}]

    # A second group in which Ola writes before Meta's confirmation arrives.
    respx.post(f"{GRAPH}/100000000000001/groups").respond(json={"messaging_product": "whatsapp"})
    await client.post("/dashboard/channels/whatsapp/group", data={"subject": "Holiday"}, headers=csrf)
    await post_whatsapp(client, wa_message(1, "first!", group_id="Z3JvdXAyZD"))
    await post_whatsapp(client, group_created(subject="Holiday", group_id="Z3JvdXAyZD"))
    assert await groups() == [{"external_thread_id": WA_GROUP, "is_primary": False},
                              {"external_thread_id": "Z3JvdXAyZD", "is_primary": True}]
