"""Dashboard: first-run setup, magic-link login, sessions, CSRF, and writes through the shared services."""
import re

from app.agent.loop import LoopRuntime
from app.core.timeutil import utcnow
from app.db import fetch_all, fetch_one, tx
from app.main import app
from app.services import members
from tests.helpers import FakeLLM, active_list, add_item, add_member, call, say, seed_home, stock_of, tg_update

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
    for path in ("/dashboard", "/dashboard/shopping", "/dashboard/inventory", "/dashboard/activity",
                 "/dashboard/playground"):
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
    posts = ["/dashboard/shopping/add", "/dashboard/playground", "/logout",
             "/dashboard/activity/actions/00000000-0000-0000-0000-000000000000/undo"]
    for path in posts:
        assert (await client.post(path, data={"item": "eggs", "text": "hi"})).status_code == 403
        wrong = await client.post(path, data={"item": "eggs", "text": "hi"}, headers={"X-CSRF-Token": "0" * 64})
        assert wrong.status_code == 403
    async with tx() as conn:
        assert await active_list(conn, home) == {}
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


async def test_health_endpoints(client):
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/readyz")).json() == {"status": "ready"}
