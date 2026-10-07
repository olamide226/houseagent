"""Subscription adapter contract: recorded runs of the real `claude` and `codex` CLIs become the
expected neutral response, and a whole turn through either one runs the household's own tools.

The recordings in fixtures/llm_cli are the CLIs' stdout as the adapters read it, trimmed of what
they never read. They are replayed by the stand-in CLI (the `cli` fixture), so nothing here
reaches a vendor. A new CLI version is checked by recording again."""
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agent.loop import LoopRuntime
from app.agent.tools import tool_definitions
from app.core.envelope import Channel, Envelope
from app.db import tx
from app.llm.claude_code import ClaudeCodeClient
from app.llm.codex_cli import CodexCliClient
from app.llm.types import ChatMessage, LLMError, TextPart, ToolDef
from tests.helpers import active_list, add_item, ctx_for, events_of, seed_home

FIXTURES = Path(__file__).parent / "fixtures" / "llm_cli"
CLIENTS = {"claude_code": ClaudeCodeClient, "codex_cli": CodexCliClient}
REQUIRED_CASES = {"tool_calls", "final_answer", "no_tools", "not_signed_in"}
CASES = [(provider, path.stem) for provider in CLIENTS for path in sorted((FIXTURES / provider).glob("*.json"))]
TOOLS = [ToolDef(name="log_inventory", description="Record stock", parameters={"type": "object"}),
         ToolDef(name="get_shopping_list", description="Read the list", parameters={"type": "object"})]
ASKED = [ChatMessage(role="user", content=[TextPart(text="Ola: we're out of eggs and bread, and what's on the list?")])]


def recording(provider: str, case: str) -> dict:
    return json.loads((FIXTURES / provider / f"{case}.json").read_text())


def with_every_tool(recorded: dict) -> dict:
    """The recording as it would be had the household's whole tool set been offered, as a turn does:
    the Claude adapter checks what the CLI says it ran with."""
    for event in recorded["stdout"]:
        if event.get("subtype") == "init":
            event["tools"] = [f"mcp__household__{tool.name}" for tool in tool_definitions(onboarding_active=False)]
    return recorded


@pytest.mark.parametrize("provider", CLIENTS)
def test_cli_has_every_required_recording(provider):
    assert {path.stem for path in (FIXTURES / provider).glob("*.json")} >= REQUIRED_CASES


@pytest.mark.parametrize("provider,case", CASES, ids=[f"{provider}-{case}" for provider, case in CASES])
async def test_recorded_run_becomes_the_expected_response(cli, provider, case):
    recorded = recording(provider, case)
    cli.replays(recorded)
    client = CLIENTS[provider](model="recorded")
    tools = [] if case == "no_tools" else TOOLS
    if "error" in recorded["expect"]:
        with pytest.raises(LLMError) as raised:
            await client.complete("rules", ASKED, tools)
        assert str(raised.value) == recorded["expect"]["error"]
        return
    response = await client.complete("rules", ASKED, tools)
    assert {"text": response.text, "stop": response.stop, "usage": response.usage.model_dump(),
            "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in response.tool_calls]} == recorded["expect"]
    assert all(c.error is None and c.id for c in response.tool_calls)


@pytest.mark.parametrize("provider", CLIENTS)
async def test_a_whole_turn_runs_the_households_tools_in_the_loop_and_hands_their_results_back(cli, provider):
    """The recorded step asks for log_inventory and get_shopping_list; the loop runs them for real."""
    cli.replays(with_every_tool(recording(provider, "tool_calls")), with_every_tool(recording(provider, "final_answer")))
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "egg", location="fridge", staple=True, qty=6)
        await add_item(conn, home, "bread", qty=1)
        said = Envelope(household_id=home.id, member_id=home.ola, member_name="Ola", thread_id=None, message_ids=[],
                        channel=Channel.telegram, scope="dm", received_at=datetime.now(UTC),
                        text="we're out of eggs and bread, and what's on the list?")
        result = await LoopRuntime(CLIENTS[provider](model="recorded")).handle(said, ctx_for(conn, home))

        assert [(c.name, c.is_error) for c in result.tool_calls] == [("log_inventory", False), ("get_shopping_list", False)]
        assert [(item, kind) for item, kind, _, _ in await events_of(conn, home)] == [("egg", "finished"), ("bread", "finished")]
        assert await active_list(conn, home) == {"egg": "finished"}
    assert result.reply == recording(provider, "final_answer")["expect"]["text"] and not result.noop
    first, second = (recording(provider, case)["expect"]["usage"]["input_tokens"] for case in ("tool_calls", "final_answer"))
    assert result.usage.input_tokens == first + second

    # The second step was asked with the first step's calls and what each one returned.
    prompt = cli.seen(1)["stdin"]
    if provider == "claude_code":
        prompt = json.loads(prompt)["message"]["content"][0]["text"]
    lines = [json.loads(line) for line in prompt.splitlines() if line.startswith('{"role"')]
    assert [line["role"] for line in lines] == ["user", "assistant", "tool", "tool"]
    assert [call["name"] for call in lines[1]["tool_calls"]] == ["log_inventory", "get_shopping_list"]
    assert [line["tool_call_id"] for line in lines[2:]] == [call["id"] for call in lines[1]["tool_calls"]]
    assert "OK: egg finished" in lines[2]["text"] and "egg" in lines[3]["text"] and not lines[2]["is_error"]
