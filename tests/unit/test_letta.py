"""The optional Letta runtime: the `/internal/tools/*` bridge and its token, and LettaRuntime
against a mocked Letta REST API (the real `letta-client`, its HTTP calls answered by respx)."""
import base64
import json
from decimal import Decimal as D

import httpx
import pytest
import respx

from app.agent import internal
from app.agent.internal import turn_in_flight
from app.agent.letta_runtime import ENV_HOUSEHOLD, ENV_TOKEN, ENV_URL, LettaRuntime, tool_source
from app.agent.loop import LOST, LoopRuntime
from app.agent.runtime import make_runtime
from app.agent.tools import REGISTRY
from app.config import get_settings
from app.core.envelope import MediaRef
from app.db import execute, fetch_all, fetch_one, fetch_val, tx
from app.llm.types import LLMError
from app.pipeline.inbound import simulate_turn
from tests.helpers import MemoryStore, add_item, ctx_for, events_of, london, seed_home, stock_of

TOKEN = "test-internal-tool-token"
LETTA = "http://letta.test:8283"
AGENT = "agent-11111111-2222-4333-8444-555555555555"
BRIDGE = "http://api.internal:8000"
BEARER = {"Authorization": f"Bearer {TOKEN}"}
EGGS_OUT = {"changes": [{"item": "eggs", "action": "finished"}]}


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(get_settings(), "internal_tool_token", TOKEN)
    internal.IN_FLIGHT.clear()


async def call(client, name, household_id, args, headers=BEARER):
    return await client.post(f"/internal/tools/{name}", json={"household_id": household_id, "args": args},
                             headers=headers)


async def message_id(conn, home):
    thread = await fetch_val(conn, "insert into threads (household_id, channel, external_thread_id, scope) "
                                   "values (:h, 'telegram', '1001', 'dm') returning id", h=home.id)
    return await fetch_val(conn, "insert into messages (household_id, thread_id, member_id, direction, text) "
                                 "values (:h, :t, :m, 'in', 'we are out of eggs') returning id",
                           h=home.id, t=thread, m=home.ola)


# ---------------------------------------------------------------- the bridge and its token
async def test_without_a_configured_token_there_is_no_bridge(client):
    async with tx() as conn:
        home = await seed_home(conn)
        with turn_in_flight(ctx_for(conn, home), False):
            for headers in ({}, BEARER, {"Authorization": "Bearer "}, {"Authorization": "Bearer None"}):
                assert (await call(client, "log_inventory", home.id, EGGS_OUT, headers)).status_code == 404
            assert await events_of(conn, home) == []


async def test_a_tool_call_without_the_token_is_refused_and_writes_nothing(client, token):
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "egg", staple=True, qty=6)
        with turn_in_flight(ctx_for(conn, home), False) as turn:
            refused = [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}, {"Authorization": f"Basic {TOKEN}"},
                       {"Authorization": f"Bearer {TOKEN}x"}, {"Authorization": f"bearer {TOKEN}"},
                       {"X-Internal-Token": TOKEN}]
            for headers in refused:
                assert (await call(client, "log_inventory", home.id, EGGS_OUT, headers)).status_code == 401
            # A body that would not even parse is refused for the missing token first.
            assert (await client.post("/internal/tools/log_inventory", json={"args": "x"})).status_code == 401
            assert turn.records == [] and await events_of(conn, home) == []
            assert await stock_of(conn, home) == {("egg", "store"): (D(6), "in_stock")}

            assert (await call(client, "log_inventory", home.id, EGGS_OUT)).status_code == 200   # the token works


async def test_a_call_runs_on_the_turn_in_flight_as_its_member_and_message(client, token):
    async with tx() as conn:
        home = await seed_home(conn)
        other = await seed_home(conn, telegram_id="2001")
        await add_item(conn, home, "egg", staple=True, qty=6)
    try:
        async with tx() as conn:
            message = await message_id(conn, home)
            with turn_in_flight(ctx_for(conn, home, message_id=message), False) as turn:
                answer = await call(client, "log_inventory", home.id, EGGS_OUT)
                assert answer.status_code == 200
                assert answer.json() == {"result": "OK: egg finished (store)\nNOTE: egg added to shopping list",
                                         "is_error": False}
                # Who and which message come from the turn, not from the caller.
                assert await fetch_one(conn, "select member_id, message_id, source, tool from agent_actions") == {
                    "member_id": home.ola, "message_id": message, "source": "agent", "tool": "log_inventory"}
                assert [(r.name, r.args, r.is_error) for r in turn.records] == [("log_inventory", EGGS_OUT, False)]
                # It was written on the turn's own transaction: nobody else can see it yet.
                async with tx() as elsewhere:
                    assert await events_of(elsewhere, home) == []

                # No turn of that household is in flight here: nothing to run it on.
                assert (await call(client, "log_inventory", other.id, EGGS_OUT)).status_code == 409
                assert (await call(client, "log_inventory", "not-a-household", EGGS_OUT)).status_code == 409
            raise RuntimeError("the turn fails after its tool call")
    except RuntimeError:
        pass
    async with tx() as conn:   # and the failed turn took the tool's writes with it
        assert await events_of(conn, home) == [] and await fetch_all(conn, "select 1 from agent_actions") == []
        assert await stock_of(conn, home) == {("egg", "store"): (D(6), "in_stock")}
    # The turn is over: a late call (a retry, a run that outlived its timeout) changes nothing.
    assert (await call(client, "log_inventory", home.id, EGGS_OUT)).status_code == 409


async def test_tool_errors_come_back_as_results_and_the_setup_tool_only_works_during_setup(client, token):
    async with tx() as conn:
        home = await seed_home(conn)
        with turn_in_flight(ctx_for(conn, home), False) as turn:
            invalid = (await call(client, "log_inventory", home.id, {"changes": [{"item": "eggs"}]})).json()
            assert invalid["is_error"] and invalid["result"].startswith("ERROR: invalid arguments")
            unknown = (await call(client, "drop_database", home.id, {})).json()
            assert unknown == {"result": "ERROR: unknown tool drop_database", "is_error": True}
            late = (await call(client, "onboarding_advance", home.id, {"step": "family"})).json()
            assert late == {"result": "ERROR: unknown tool onboarding_advance", "is_error": True}
            assert [r.is_error for r in turn.records] == [True, True, True]
        await execute(conn, """update households set onboarding_state = '{"step": "family", "done": []}'""")
        with turn_in_flight(ctx_for(conn, home), True):
            during = (await call(client, "onboarding_advance", home.id, {"step": "family"})).json()
            assert during["is_error"] is False and during["result"].startswith("OK: family done")


# ---------------------------------------------------------------- the runtime against a mocked Letta API
def letta_api(*replies, during=None):
    """Mock the five Letta endpoints the runtime uses. `replies` are what the agent says, turn by
    turn; `during` is what Letta does while it runs a turn (call our tools)."""
    routes = {
        "tool": respx.put(f"{LETTA}/v1/tools/").mock(side_effect=lambda request: httpx.Response(
            200, json={"id": "tool-" + json.loads(request.content)["json_schema"]["name"]})),
        "create": respx.post(f"{LETTA}/v1/agents/").respond(json={"id": AGENT}),
        "block": respx.patch(url__regex=rf"{LETTA}/v1/agents/{AGENT}/core-memory/blocks/(persona|household)").respond(
            json={"id": "block-1"}),
        "update": respx.patch(f"{LETTA}/v1/agents/{AGENT}").respond(json={"id": AGENT}),
    }
    script = list(replies)

    async def turn(request):
        if during:
            await during(json.loads(request.content))
        reply = script.pop(0)
        if isinstance(reply, httpx.Response):
            return reply
        said = [reply] if isinstance(reply, str) else reply or []   # an agent may speak before a tool call too
        messages = [{"message_type": "reasoning_message", "reasoning": "thinking it over"},
                    *({"message_type": "assistant_message", "content": text} for text in said)]
        return httpx.Response(200, json={
            "messages": messages,
            "stop_reason": {"stop_reason": "end_turn" if reply is not None else "max_steps"},
            "usage": {"prompt_tokens": 2100, "completion_tokens": 40, "cached_input_tokens": 1500, "step_count": 2}})

    routes["message"] = respx.post(f"{LETTA}/v1/agents/{AGENT}/messages").mock(side_effect=turn)
    return routes


def runtime(**kwargs) -> LettaRuntime:
    from letta_client import AsyncLetta

    return LettaRuntime(AsyncLetta(base_url=LETTA, max_retries=0), tool_url=BRIDGE + "/", tool_token=TOKEN,
                        model="openai-proxy/some-model", **kwargs)


def sent(route, n=-1) -> dict:
    return json.loads(route.calls[n].request.content)


@respx.mock
async def test_the_first_turn_creates_the_households_agent_with_our_tools_blocks_and_tool_environment(client, token):
    async def letta_calls_our_tool(request):
        answer = await call(client, "log_inventory", home.id, EGGS_OUT)
        assert answer.json()["is_error"] is False

    api = letta_api("ACK", during=letta_calls_our_tool)
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "egg", staple=True, qty=6)
        result = await simulate_turn(conn, runtime(agent_name="Hearth"), home.id, home.ola, "we're out of eggs",
                                     now=london("2026-10-05 12:00"))
        assert await events_of(conn, home) == [("egg", "finished", None, "message")]

    assert (result.ack_only, result.reply, result.noop) == (True, None, False)
    assert [(r.name, r.args) for r in result.tool_calls] == [("log_inventory", EGGS_OUT)]
    assert result.usage.model_dump() == {"input_tokens": 2100, "output_tokens": 40, "cached_tokens": 1500}

    # All twelve tools, by name, each a function Letta can run and the schema the loop offers.
    registered = [json.loads(c.request.content) for c in api["tool"].calls]
    assert [t["json_schema"]["name"] for t in registered] == list(REGISTRY)
    assert all(f"def {t['json_schema']['name']}(" in t["source_code"] and t["json_schema"]["description"]
               for t in registered)
    assert registered[0]["json_schema"]["parameters"] == REGISTRY["log_inventory"].args.model_json_schema()

    created = sent(api["create"])
    assert created["name"] == f"household-{home.id}" and created["model"] == "openai-proxy/some-model"
    assert created["include_base_tools"] is False                   # Postgres is the memory, not Letta's own tools
    assert created["tool_ids"] == [f"tool-{name}" for name in REGISTRY]
    assert created["secrets"] == {ENV_HOUSEHOLD: home.id, ENV_URL: BRIDGE, ENV_TOKEN: TOKEN}
    blocks = {block["label"]: block["value"] for block in created["memory_blocks"]}
    assert list(blocks) == ["persona", "household"]
    assert blocks["persona"].startswith("You are Hearth, the household assistant") and "reply with exactly ACK" in blocks["persona"]
    assert "Now: Monday 5 Oct 2026 12:00 (Europe/London)" in blocks["household"] and "Family: Ola (adult)" in blocks["household"]
    assert "egg" not in blocks["persona"] and "cache-break" not in "".join(blocks.values())

    # One user message with the turn's text. No thread history: Letta keeps its own.
    message = sent(api["message"])
    assert message["messages"] == [{"role": "user", "content": [{"type": "text", "text": "we're out of eggs"}]}]
    assert message["max_steps"] == 8
    async with tx() as conn:
        assert await fetch_val(conn, "select letta_agent_id from households where id = :h", h=home.id) == AGENT
    assert not api["block"].called and not api["update"].called


@respx.mock
async def test_later_turns_rewrite_both_blocks_and_the_tool_environment_and_reuse_the_agent(client, token):
    api = letta_api("ACK", ["Let me look.", "Eggs and bread."], "NOOP", None)
    one = runtime()
    async with tx() as conn:
        home = await seed_home(conn)
        await simulate_turn(conn, one, home.id, home.ola, "hello", now=london("2026-10-05 12:00"))
    async with tx() as conn:
        await add_item(conn, home, "plantain", location="store", status="low")           # the state moves on in Postgres
        reply = await simulate_turn(conn, one, home.id, home.ola, "what do we need?", now=london("2026-10-05 12:01"))
        noop = await simulate_turn(conn, one, home.id, home.ola, "love you, see you at 6", scope="group",
                                   now=london("2026-10-05 12:02"))
        lost = await simulate_turn(conn, one, home.id, home.ola, "and then?", now=london("2026-10-05 12:03"))

    assert api["create"].call_count == 1 and api["tool"].call_count == 12          # once per process, not per turn
    assert api["message"].call_count == 4
    assert [(c.request.url.path.rsplit("/", 1)[1]) for c in api["block"].calls[:2]] == ["persona", "household"]
    household = json.loads(api["block"].calls[1].request.content)["value"]
    assert "Low or out: plantain (low)" in household and "Now: Monday 5 Oct 2026 12:01" in household
    assert sent(api["update"], 0) == {"secrets": {ENV_HOUSEHOLD: home.id, ENV_URL: BRIDGE, ENV_TOKEN: TOKEN}}
    assert api["block"].call_count == 6 and api["update"].call_count == 3
    assert "Speaking: Ola (dashboard, group)" in json.loads(api["block"].calls[3].request.content)["value"]

    assert (reply.reply, reply.ack_only, reply.noop) == ("Eggs and bread.", False, False)   # its last words
    assert (noop.reply, noop.noop) == (None, True)
    assert lost.reply == LOST                                                       # out of steps with nothing said

    # A second process (the api's Playground beside the worker) registers the tools for itself,
    # points the agent's tools at its own bridge, and still uses the household's one agent.
    api["message"].side_effect = None
    api["message"].respond(json={"messages": [{"message_type": "assistant_message", "content": [
        {"type": "text", "text": "On "}, {"type": "text", "text": "the list."}]}],
        "stop_reason": {"stop_reason": "end_turn"}, "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    other = LettaRuntime(one._client, tool_url="http://worker.internal:8001", tool_token="rotated-token")
    async with tx() as conn:
        parts = await simulate_turn(conn, other, home.id, home.ola, "add salt")
    assert parts.reply == "On the list." and api["create"].call_count == 1 and api["tool"].call_count == 24
    assert sent(api["update"])["secrets"] == {ENV_HOUSEHOLD: home.id, ENV_URL: "http://worker.internal:8001",
                                              ENV_TOKEN: "rotated-token"}


@respx.mock
async def test_a_photo_goes_to_letta_as_an_image_part_and_a_model_without_images_is_told_instead(client, token):
    api = letta_api("ACK", "ACK")
    store = MemoryStore()
    store.objects["receipt.jpg"] = b"\xff\xd8receipt"
    photo = MediaRef(kind="image", mime="image/jpeg", storage_backend="s3", storage_key="receipt.jpg")
    async with tx() as conn:
        home = await seed_home(conn)
        await simulate_turn(conn, runtime(media=store), home.id, home.ola, "Tesco receipt", photos=[photo])
        await simulate_turn(conn, runtime(media=store, supports_images=False), home.id, home.ola, None, photos=[photo])
    assert sent(api["message"], 0)["messages"][0]["content"] == [
        {"type": "text", "text": "Tesco receipt\n[photo]"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                     "data": base64.b64encode(b"\xff\xd8receipt").decode()}}]
    assert sent(api["message"], 1)["messages"][0]["content"] == [
        {"type": "text", "text": "[photo]\n[photo received; this model can't read photos]"}]


@respx.mock
async def test_a_letta_failure_fails_the_turn_without_repeating_what_letta_said(client, token):
    api = letta_api(httpx.Response(500, json={"detail": f"tool env was HOUSEAGENT_TOOL_TOKEN={TOKEN}"}))
    async with tx() as conn:
        home = await seed_home(conn)
        with pytest.raises(LLMError) as failed:
            await simulate_turn(conn, runtime(), home.id, home.ola, "hello")
        assert TOKEN not in str(failed.value) and failed.value.__cause__ is None
        assert str(failed.value) == "letta: InternalServerError"
    assert internal.IN_FLIGHT == {}                                                 # nothing left in flight
    respx.post(f"{LETTA}/v1/agents/").mock(side_effect=httpx.ConnectError("refused"))
    async with tx() as conn:
        home = await seed_home(conn, telegram_id="2001")
        with pytest.raises(LLMError, match="letta: APIConnectionError"):
            await simulate_turn(conn, runtime(), home.id, home.ola, "hello")
    assert api["message"].call_count == 1


# ---------------------------------------------------------------- the generated tools and the factory
def test_each_generated_tool_posts_its_arguments_to_the_bridge_with_the_token(monkeypatch):
    seen = []

    class Answer:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            return json.dumps({"result": "OK: recorded", "is_error": False}).encode()

    def urlopen(request, timeout):
        seen.append((request.full_url, dict(request.header_items()), json.loads(request.data), request.get_method()))
        return Answer()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    for name, value in ((ENV_HOUSEHOLD, "h-1"), (ENV_URL, BRIDGE), (ENV_TOKEN, TOKEN)):
        monkeypatch.setenv(name, value)
    for name, spec in REGISTRY.items():
        namespace: dict = {}
        exec(compile(tool_source(spec), f"<{name}>", "exec"), namespace)   # what Letta's tool sandbox does
        function = namespace[name]
        properties = spec.args.model_json_schema()["properties"]
        assert set(function.__code__.co_varnames[:function.__code__.co_argcount]) == set(properties)

    assert namespace is not None
    exec(compile(tool_source(REGISTRY["update_shopping_list"]), "<tool>", "exec"), namespace)
    assert namespace["update_shopping_list"](add=["eggs", "bread"], remove=None) == "OK: recorded"
    url, headers, body, method = seen[-1]
    assert (url, method) == (f"{BRIDGE}/internal/tools/update_shopping_list", "POST")
    assert headers["Authorization"] == f"Bearer {TOKEN}" and headers["Content-type"] == "application/json"
    assert body == {"household_id": "h-1", "args": {"add": ["eggs", "bread"]}}      # what was not given is not sent


def test_the_loop_is_the_default_runtime_and_letta_needs_its_settings(monkeypatch):
    settings = get_settings().model_copy()
    assert settings.agent_runtime == "loop" and isinstance(make_runtime(settings, None), LoopRuntime)

    settings.agent_runtime = "letta"
    for missing in ({"letta_base_url": None, "internal_tool_token": TOKEN},
                    {"letta_base_url": LETTA, "internal_tool_token": None}):
        for name, value in missing.items():
            setattr(settings, name, value)
        with pytest.raises(ValueError, match="LETTA_BASE_URL and INTERNAL_TOOL_TOKEN"):
            make_runtime(settings, None)
    settings.letta_base_url, settings.internal_tool_token = LETTA, TOKEN
    api_side = make_runtime(settings, None)
    worker_side = make_runtime(settings, None, tool_url=settings.worker_internal_url)
    assert isinstance(api_side, LettaRuntime) and api_side._tool_url == "http://testserver"   # PUBLIC_BASE_URL
    assert isinstance(worker_side, LettaRuntime) and worker_side._tool_url == "http://localhost:8001"
    settings.internal_base_url = "http://household-agent-api:8000"
    assert make_runtime(settings, None)._tool_url == "http://household-agent-api:8000"
