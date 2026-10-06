"""The System page: admins only, the worker's heartbeat, job runs, model and version, the last
eval result, the stock check with its rebuild, and the household export."""
import asyncio
import json
import re
from datetime import timedelta
from decimal import Decimal as D

from app.agent.tools import run_tool
from app.config import get_settings
from app.core.timeutil import utcnow
from app.db import execute, fetch_all, fetch_val, tx
from app.services import households, members
from app.worker import main as worker
from tests.helpers import add_item, add_member, ctx_for, seed_home, stock_snapshot
from tests.unit.test_dashboard import login


async def page(client, path="/dashboard/system") -> str:
    response = await client.get(path)
    assert response.status_code == 200
    return re.sub(r"\s+", " ", response.text)


async def log(conn, home, item, action, **extra):
    result, is_error = await run_tool("log_inventory", {"changes": [{"item": item, "action": action, **extra}]},
                                      ctx_for(conn, home))
    assert not is_error, result


# ---------------------------------------------------------------- who may see it
async def test_only_an_admin_sees_the_system_page_its_export_and_its_rebuild(client):
    async with tx() as conn:
        home = await seed_home(conn)
        ada = await add_member(conn, home, "Ada", telegram_id="1002")           # an adult, not the admin
    assert (await client.get("/dashboard/system")).status_code == 401           # nobody is logged in

    csrf = await login(client, home, ada)
    assert (await client.get("/dashboard/system")).status_code == 403
    assert (await client.get("/dashboard/system/export")).status_code == 403
    assert (await client.post("/dashboard/system/rebuild-stock", headers=csrf)).status_code == 403
    assert 'href="/dashboard/system"' not in (await client.get("/dashboard")).text

    await login(client, home)
    assert (await client.get("/dashboard/system")).status_code == 200
    assert 'href="/dashboard/system"' in (await client.get("/dashboard")).text


# ---------------------------------------------------------------- worker, jobs, model, version
async def test_the_page_says_whether_the_worker_is_alive_and_lists_this_households_job_runs(client):
    now = utcnow()
    async with tx() as conn:
        home = await seed_home(conn)
        other = await seed_home(conn, telegram_id="2001")
        await execute(conn, "insert into job_runs (job, household_id, run_key, ran_at) values "
                            "('daily_brief', :h, '2026-10-05', :at), ('weekly_digest', :h, '2026-W40', :at), "
                            "('low_stock_prompt', :other, '2026-10-04', :at)",
                      h=home.id, other=other.id, at=now - timedelta(hours=5))
    await login(client, home)
    assert "The worker has not reported yet" in await page(client)

    async with tx() as conn:
        await households.beat(conn, now - timedelta(minutes=2, seconds=30))     # a missed beat or two is not an alarm
    text = await page(client)
    assert "Running. Last reported" in text and "probably not running" not in text
    assert "daily_brief <span class=\"muted\">2026-10-05</span>" in text and "weekly_digest" in text
    assert "low_stock_prompt" not in text                                       # the other household's
    assert "worker_heartbeat" not in text                                       # shown as a sentence, not as a job

    async with tx() as conn:
        await households.beat(conn, now - timedelta(minutes=4))                 # the same row, moved, never a second
        assert await fetch_val(conn, "select count(*) from job_runs where job = 'worker_heartbeat'") == 2   # one each
    assert "It reports every minute, so it is probably not running" in await page(client)


async def test_the_page_names_the_model_and_version_and_never_shows_a_secret(client):
    async with tx() as conn:
        home = await seed_home(conn)
    await login(client, home)
    text = await page(client)
    assert "<span>loop</span>" in text
    assert 'openai_compat <span class="muted">at llm.test</span>' in text and "<span>test-model</span>" in text
    assert "0.1.0" in text and "database at migration 0001" in text
    settings = get_settings()
    for secret in (settings.llm_api_key, settings.session_secret, settings.setup_token, settings.tg_bot_token,
                   settings.tg_webhook_secret, settings.wa_access_token, settings.wa_app_secret,
                   settings.bb_password, settings.bb_webhook_secret, settings.database_url):
        assert secret and secret not in text


async def test_the_last_eval_result_is_read_from_where_the_suite_leaves_it(client, tmp_path, monkeypatch):
    async with tx() as conn:
        home = await seed_home(conn)
    await login(client, home)
    monkeypatch.setattr(get_settings(), "eval_results_dir", str(tmp_path / "nowhere"))
    assert "No eval result on this machine" in await page(client)

    monkeypatch.setattr(get_settings(), "eval_results_dir", str(tmp_path))
    (tmp_path / "openai_compat.json").write_text(json.dumps({
        "provider": "openai_compat", "model": "some-model", "passed": 48, "total": 50, "usage": {},
        "cases": {"undo:undo a list change": "passed", "inventory:batch finish with staple": "failed",
                  "photos:long receipt": "failed", **{f"noop:{n}": "passed" for n in range(47)}}}))
    (tmp_path / "anthropic.json").write_text(json.dumps({
        "provider": "anthropic", "model": "other-model", "passed": 3, "total": 3, "usage": {},
        "cases": {"noop:a": "passed", "noop:b": "passed", "noop:c": "passed"}}))
    (tmp_path / "notes.json").write_text('["not a result"]')
    (tmp_path / "broken.json").write_text("{")
    text = await page(client)
    assert '<span class="low">48 of 50 passed</span>' in text and "some-model" in text
    assert "<pre>inventory:batch finish with staple\nphotos:long receipt</pre>" in (await client.get("/dashboard/system")).text
    assert "<span>3 of 3 passed</span>" in text and "other-model" in text
    assert text.count("passed</span>") == 2 and "No eval result" not in text


# ---------------------------------------------------------------- the stock check and rebuild
async def test_the_stock_check_finds_what_differs_from_the_log_and_the_rebuild_puts_it_right(client):
    async with tx() as conn:
        home = await seed_home(conn)
        other = await seed_home(conn, telegram_id="2001")
        for house in (home, other):
            await log(conn, house, "eggs", "adjusted", quantity=12, location="fridge")
            await log(conn, house, "eggs", "used", quantity=2)
            await log(conn, house, "rice", "adjusted", quantity=3)
            await log(conn, house, "rice", "finished")
            await log(conn, house, "milk", "restocked", quantity=2, location="fridge")
        truth = await stock_snapshot(conn, home)
        await add_item(conn, home, "yam")                                       # never stocked: nothing to differ
    csrf = await login(client, home)
    assert "Stock matches the event log: 3 rows checked." in await page(client)

    async def break_stock(house):
        async with tx() as conn:   # what a bad migration or a hand-typed UPDATE would leave behind
            await execute(conn, "update stock set qty_estimate = 99 where item_id = "
                                "(select id from items where household_id = :h and canonical_name = 'egg')", h=house.id)
            await execute(conn, "update stock set status = 'in_stock' where item_id = "
                                "(select id from items where household_id = :h and canonical_name = 'rice')", h=house.id)
            await execute(conn, "delete from stock where item_id = "
                                "(select id from items where household_id = :h and canonical_name = 'milk')", h=house.id)

    await break_stock(home)
    await break_stock(other)
    async with tx() as conn:
        await add_item(conn, home, "ghost", qty=4)                              # a stock row no event ever made
        theirs = await stock_snapshot(conn, other)
    text = await page(client)
    assert "Stock differs from the event log in 4 places." in text
    assert 'egg <span class="muted">fridge</span></span> <span>shown as in stock, 99; the log says in stock, 10' in text
    assert 'rice <span class="muted">store</span></span> <span>shown as in stock, 0; the log says out, 0' in text
    assert 'milk <span class="muted">fridge</span></span> <span>shown as no row; the log says in stock' in text
    assert 'ghost <span class="muted">store</span></span> <span>shown as in stock, 4; the log says no row' in text

    assert (await client.post("/dashboard/system/rebuild-stock")).status_code == 403          # no CSRF token
    rebuilt = await client.post("/dashboard/system/rebuild-stock", headers=csrf)
    assert rebuilt.status_code == 202
    assert "Stock rebuilt from the event log: 4 rows changed." in rebuilt.text
    assert "Stock matches the event log: 3 rows checked." in re.sub(r"\s+", " ", rebuilt.text)
    async with tx() as conn:
        assert await stock_snapshot(conn, home) == truth
        assert await stock_snapshot(conn, other) == theirs                      # the other household is not ours to fix
        assert await fetch_all(conn, "select source, tool, member_id from agent_actions where tool like 'system.%'") == [
            {"source": "dashboard", "tool": "system.rebuild_stock", "member_id": home.ola}]
        events_before = await fetch_val(conn, "select count(*) from inventory_events")

    again = await client.post("/dashboard/system/rebuild-stock", headers=csrf)                 # nothing to do: no action logged
    assert again.status_code == 202 and "0 rows changed" in again.text
    async with tx() as conn:
        assert await fetch_val(conn, "select count(*) from agent_actions where tool like 'system.%'") == 1
        assert await fetch_val(conn, "select count(*) from inventory_events") == events_before   # the log is only read
        assert await stock_snapshot(conn, home) == truth


# ---------------------------------------------------------------- export
async def test_the_export_holds_every_table_of_this_household_and_nothing_secret_or_anyone_elses(client):
    async with tx() as conn:
        home = await seed_home(conn)
        await add_member(conn, home, "Tobi", role="child")
        other = await seed_home(conn, telegram_id="2001")
        for house, item in ((home, "eggs"), (other, "caviar")):
            await log(conn, house, item, "added", quantity=6)
        invite = await members.create_invite(conn, home.ola, utcnow())
        presence = await members.new_presence_token(conn, home.ola)
        calendar = await households.new_calendar_token(conn, home.id)
        login_token = await members.create_login_token(conn, home.ola, utcnow())
        await households.beat(conn, utcnow())
        every_table = {r["table_name"] for r in await fetch_all(
            conn, "select table_name from information_schema.tables where table_schema = 'public'")}
    # A table added to the schema later must be exported or left out on purpose.
    assert set(households.EXPORTED) | {"login_tokens", "alembic_version"} == every_table
    await login(client, home)

    response = await client.get("/dashboard/system/export")
    assert response.status_code == 200 and response.headers["content-type"] == "application/json"
    assert response.headers["content-disposition"] == 'attachment; filename="household-export.json"'
    export = response.json()
    assert export["household"] == "Adebayo" and export["version"] == "0.1.0" and export["exported_at"]
    tables = export["tables"]
    assert list(tables) == list(households.EXPORTED) and "login_tokens" not in tables
    assert [m["name"] for m in tables["members"]] and {m["name"] for m in tables["members"]} == {"Ola", "Tobi"}
    assert [h["id"] for h in tables["households"]] == [home.id]
    assert [i["canonical_name"] for i in tables["items"]] == ["egg"]
    assert [(e["event_type"], D(str(e["quantity"]))) for e in tables["inventory_events"]] == [("added", D(6))]
    assert len(tables["stock"]) == 1 and len(tables["channel_identities"]) == 1 and len(tables["agent_actions"]) == 1
    assert {row["household_id"] for name in ("members", "items", "locations", "agent_actions", "job_runs")
            for row in tables[name]} == {home.id}

    text = response.text
    assert other.id not in text and "caviar" not in text
    assert "_hash" not in text
    for secret in (invite, presence, calendar, login_token):
        assert secret not in text


# ---------------------------------------------------------------- the worker's side of the heartbeat
async def test_the_worker_heartbeat_touches_its_file_and_reports_to_every_household(tmp_path, monkeypatch):
    async with tx() as conn:
        home = await seed_home(conn)
    beat_file = tmp_path / "worker-heartbeat"
    monkeypatch.setattr(worker, "HEARTBEAT_SECONDS", 0.05)
    task = asyncio.create_task(worker.heartbeat(beat_file))
    try:
        for _ in range(100):
            await asyncio.sleep(0.05)
            async with tx() as conn:
                seen, runs = await households.job_runs(conn, home.id)
            if seen and beat_file.exists():
                break
        assert beat_file.exists() and seen is not None and utcnow() - seen < timedelta(seconds=10) and runs == []

        # The database goes away: the file the liveness probe watches must keep moving.
        async def away(conn, now):
            raise OSError("database is away")

        monkeypatch.setattr(households, "beat", away)
        beat_file.unlink()
        for _ in range(100):
            await asyncio.sleep(0.05)
            if beat_file.exists():
                break
        assert beat_file.exists() and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
