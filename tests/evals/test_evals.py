"""Agent evals: real models, judged by database state (spec section 16).

Each YAML case seeds a household, sends its turns through simulate_turn(), the same
service the dashboard Playground uses, and asserts on rows. The clock is pinned (Monday
5 October 2026, 12:00 in London, unless a case says otherwise), so "on Wednesday" means the
same date whenever the suite runs. Skipped unless RUN_EVALS=1:

    RUN_EVALS=1 EVAL_API_KEY=... EVAL_OPENAI_BASE_URL=... EVAL_ANTHROPIC_BASE_URL=... \
        EVAL_MODEL=... uv run pytest tests/evals -m eval
"""
import json
import os
from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.agent.actions import Recorder
from app.agent.loop import LoopRuntime
from app.agent.resolve import normalise
from app.db import fetch_all, tx
from app.llm.anthropic import AnthropicClient
from app.llm.openai_compat import OpenAICompatClient
from app.llm.types import Usage
from app.pipeline.inbound import simulate_turn
from app.services import calendar, shopping
from tests.helpers import add_item, add_member, ctx_for, london, seed_home, wall

pytestmark = [pytest.mark.eval, pytest.mark.skipif(os.environ.get("RUN_EVALS") != "1", reason="set RUN_EVALS=1")]

HERE = Path(__file__).parent
RESULTS = HERE / ".results"
API_KEY = os.environ.get("EVAL_API_KEY") or os.environ.get("DEEPSEEK_API_KEY", "")
MODEL = os.environ.get("EVAL_MODEL", "deepseek-flash")
NOW = "2026-10-05 12:00"
ENDPOINTS = {
    "openai_compat": os.environ.get("EVAL_OPENAI_BASE_URL") or os.environ.get("DEEPSEEK_OPENAI_BASE_URL"),
    "anthropic": os.environ.get("EVAL_ANTHROPIC_BASE_URL") or os.environ.get("DEEPSEEK_ANTHROPIC_BASE_URL"),
}
CASES = [
    (path.stem, case) for path in sorted(HERE.glob("*.yaml")) for case in yaml.safe_load(path.read_text())
]
usage_by_provider: dict[str, Usage] = {}
outcomes: dict[str, dict[str, str]] = {}


def make_runtime(provider: str) -> LoopRuntime:
    client_class = AnthropicClient if provider == "anthropic" else OpenAICompatClient
    return LoopRuntime(client_class(api_key=API_KEY, model=MODEL, base_url=ENDPOINTS[provider]))


async def seed(case: dict[str, Any], now: datetime) -> Any:
    spec = case.get("seed", {})
    async with tx() as conn:
        home = await seed_home(conn)
        await add_member(conn, home, "Ada", telegram_id="1002")
        for member in spec.get("members", []):
            await add_member(conn, home, member["name"], role=member.get("role", "adult"))
        for event in spec.get("events", []):
            # Through the service, so seeded events have their reminders, but with no undo record.
            await calendar.schedule_event(
                Recorder(ctx_for(conn, home, now=now)), title=event["title"], starts_at=london(event["starts_at"]),
                rrule=event.get("rrule"), location=event.get("location"),
                participant_ids=[home.members[name] for name in event.get("participants", [])])
        ids = {}
        for item in spec.get("items", []):
            stock = item.get("stock", {})
            location = next(iter(stock), item.get("location", "store"))
            ids[item["name"]] = await add_item(
                conn, home, item["name"], location=location, staple=item.get("staple", False),
                qty=stock.get(location), status=None if stock.get(location) is None else
                ("out" if stock[location] == 0 else "in_stock"))
        for name in spec.get("list", []):
            await shopping.add_entry(_recorder(conn, home), ids[name], "explicit")
    return home


def _recorder(conn: Any, home: Any) -> Recorder:
    return Recorder(ctx_for(conn, home))


async def observed(home: Any) -> dict[str, Any]:
    async with tx() as conn:
        events = await fetch_all(
            conn,
            """select lower(i.canonical_name) as item, e.event_type as type, e.quantity, l.name as location
               from inventory_events e join items i on i.id = e.item_id
               left join locations l on l.id = e.location_id
               where e.household_id = :h and e.source <> 'undo' order by e.occurred_at""", h=home.id)
        stock = await fetch_all(
            conn,
            """select lower(i.canonical_name) as item, l.name as location, s.qty_estimate as qty, s.status
               from stock s join items i on i.id = s.item_id join locations l on l.id = s.location_id
               where i.household_id = :h""", h=home.id)
        active = await fetch_all(
            conn,
            """select lower(i.canonical_name) as item from shopping_list_items s
               join items i on i.id = s.item_id where s.household_id = :h and s.status = 'needed'""", h=home.id)
        actions = await fetch_all(conn, "select tool from agent_actions where household_id = :h", h=home.id)
        booked = await fetch_all(
            conn,
            """select e.title, e.status, e.starts_at, e.rrule, e.exdates, e.location,
                      coalesce((select array_agg(m.name) from members m where m.id = any(e.participant_ids)),
                               '{}') as participants
               from events e where e.household_id = :h order by e.starts_at""", h=home.id)
        reminders = await fetch_all(
            conn,
            """select r.text, r.fire_at, r.rrule, r.target, r.urgency, r.event_id, m.name as member
               from reminders r left join members m on m.id = r.member_id
               where r.household_id = :h and r.status = 'scheduled' order by r.fire_at""", h=home.id)
    return {"events": events, "stock": stock, "active": sorted(row["item"] for row in active), "actions": actions,
            "calendar": booked, "reminders": reminders}


def key(name: str) -> str:
    return normalise(name).lower()


def check(expect: dict[str, Any], seen: dict[str, Any], seeded_list: list[str], last_reply: str) -> None:
    if "events" in expect:
        assert len(seen["events"]) == len(expect["events"]), f"events: {seen['events']}"
        remaining = list(seen["events"])
        for wanted in expect["events"]:
            types = wanted["type"] if isinstance(wanted["type"], list) else [wanted["type"]]
            match = next((e for e in remaining if e["item"] == key(wanted["item"]) and e["type"] in types
                          and ("quantity" not in wanted or e["quantity"] == _decimal(wanted["quantity"]))
                          and ("location" not in wanted or e["location"] == wanted["location"])), None)
            assert match is not None, f"no event like {wanted} in {seen['events']}"
            remaining.remove(match)
    if "shopping_list_active" in expect:
        assert seen["active"] == sorted(key(name) for name in expect["shopping_list_active"])
    for name, wanted in expect.get("stock", {}).items():
        rows = [row for row in seen["stock"] if row["item"] == key(name)
                and ("location" not in wanted or row["location"] == wanted["location"])]
        assert len(rows) == 1, f"stock rows for {name}: {seen['stock']}"
        assert rows[0]["status"] == wanted["status"], rows[0]
        if "qty" in wanted:
            assert rows[0]["qty"] == _decimal(wanted["qty"]), rows[0]
    if expect.get("writes") == 0:
        assert seen["events"] == [] and seen["actions"] == []
        assert Counter(seen["active"]) == Counter(key(name) for name in seeded_list)
    if "reply" in expect:
        kind = last_reply if last_reply in ("ACK", "NOOP") else "text"
        assert kind == expect["reply"], f"reply was {last_reply!r}"
    active = [event for event in seen["calendar"] if event["status"] == "active"]
    if "calendar" in expect:
        _match_all("calendar event", expect["calendar"], active, _event_matches)
    if "calendar_cancelled" in expect:
        assert len(seen["calendar"]) - len(active) == expect["calendar_cancelled"], seen["calendar"]
    if "reminders_scheduled" in expect:
        assert len(seen["reminders"]) == expect["reminders_scheduled"], seen["reminders"]
    if "reminders" in expect:
        standalone = [reminder for reminder in seen["reminders"] if reminder["event_id"] is None]
        _match_all("reminder", expect["reminders"], standalone, _reminder_matches)


def _match_all(kind: str, wanted: list[dict[str, Any]], rows: list[dict[str, Any]], matches: Any) -> None:
    """Exactly these rows, in any order."""
    assert len(rows) == len(wanted), f"{kind}s: {rows}"
    remaining = list(rows)
    for want in wanted:
        match = next((row for row in remaining if matches(want, row)), None)
        assert match is not None, f"no {kind} like {want} in {rows}"
        remaining.remove(match)


def _contains(wanted: str | list[str] | None, text: str | None) -> bool:
    parts = [wanted] if isinstance(wanted, str) else wanted or []
    return all(part.lower() in (text or "").lower() for part in parts)


def _event_matches(want: dict[str, Any], event: dict[str, Any]) -> bool:
    return (
        _contains(want.get("title_contains"), event["title"])
        and ("participants" not in want or sorted(event["participants"]) == sorted(want["participants"]))
        and ("local_start" not in want or wall(event["starts_at"]) == want["local_start"])
        and ("rrule_contains" not in want or (event["rrule"] and _contains(want["rrule_contains"], event["rrule"])))
        and (want.get("repeats", True) or event["rrule"] is None)
        and _contains(want.get("location_contains"), event["location"])
        and ("exdates" not in want or event["exdates"] == [date.fromisoformat(day) for day in want["exdates"]])
    )


def _reminder_matches(want: dict[str, Any], reminder: dict[str, Any]) -> bool:
    return (
        _contains(want.get("text_contains"), reminder["text"])
        and ("local_fire" not in want or wall(reminder["fire_at"]) == want["local_fire"])
        and all(reminder[name] == want[name] for name in ("target", "member", "urgency") if name in want)
        and ("rrule_contains" not in want
             or (reminder["rrule"] and _contains(want["rrule_contains"], reminder["rrule"])))
        and (want.get("repeats", True) or reminder["rrule"] is None)
    )


def _decimal(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


@pytest.mark.parametrize("provider", list(ENDPOINTS))
@pytest.mark.parametrize("suite,case", CASES, ids=[f"{suite}:{case['name']}" for suite, case in CASES])
async def test_case(provider: str, suite: str, case: dict[str, Any]) -> None:
    if not (API_KEY and ENDPOINTS[provider]):
        pytest.skip(f"no credentials for {provider}")
    now = london(case.get("now", NOW))
    home = await seed(case, now)
    runtime = make_runtime(provider)
    reply = ""
    outcomes.setdefault(provider, {})[f"{suite}:{case['name']}"] = "failed"
    for minutes, turn in enumerate(case["turns"]):
        async with tx() as conn:
            result = await simulate_turn(conn, runtime, home.id, home.members[turn["from"]], turn["text"],
                                         scope=turn.get("scope", "dm"), now=now + timedelta(minutes=minutes))
        usage_by_provider[provider] = usage_by_provider.get(provider, Usage()) + result.usage
        reply = "ACK" if result.ack_only else "NOOP" if result.noop else (result.reply or "")
    check(case["expect"], await observed(home), case.get("seed", {}).get("list", []), reply)
    outcomes[provider][f"{suite}:{case['name']}"] = "passed"


@pytest.fixture(scope="module", autouse=True)
def _write_results():
    yield
    RESULTS.mkdir(exist_ok=True)
    for provider, cases in outcomes.items():
        passed = sum(1 for outcome in cases.values() if outcome == "passed")
        (RESULTS / f"{provider}.json").write_text(json.dumps({
            "provider": provider, "model": MODEL, "passed": passed, "total": len(cases),
            "usage": usage_by_provider.get(provider, Usage()).model_dump(), "cases": cases,
        }, indent=2) + "\n")
