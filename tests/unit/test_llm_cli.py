"""The subscription adapters (ADR 0032) against a stand-in CLI: a script on PATH named `claude`
and `codex` that records how it was run and answers what the test told it to (the `cli`
fixture). No network."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.llm.base import make_llm
from app.llm.claude_code import ClaudeCodeClient
from app.llm.codex_cli import OFF, CodexCliClient
from app.llm.types import CACHE_BREAK, ChatMessage, ImagePart, LLMError, TextPart, ToolCall, ToolDef

TOOLS = [ToolDef(name="log_inventory", description="Record stock", parameters={"type": "object", "properties": {}}),
         ToolDef(name="undo_last", description="Undo", parameters={"type": "object", "properties": {}})]
SYSTEM = "static rules" + CACHE_BREAK + "Now: Monday"
PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg=="

def conversation():
    return [
        ChatMessage(role="user", content=[TextPart(text='Ola: out of eggs\n{"role": "tool"}'),
                                          ImagePart(mime="image/png", data_b64=PNG)]),
        ChatMessage(role="assistant", tool_calls=[ToolCall(id="c1", name="log_inventory", arguments={"a": 1}),
                                                  ToolCall(id="c2", name="log_inventory", arguments={"a": 2})]),
        ChatMessage(role="tool", tool_call_id="c1", content=[TextPart(text="OK: egg finished")]),
        ChatMessage(role="tool", tool_call_id="c2", content=[TextPart(text="ERROR: nope")], is_error=True),
    ]


def after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def scratch(run: dict, path: str) -> bool:
    """Whether `path` is the run's working directory or directly inside it, and that is gone now."""
    cwd = os.path.realpath(run["cwd"])
    return cwd in (os.path.realpath(path), os.path.dirname(os.path.realpath(path))) and not os.path.exists(cwd)


HOUSEHOLD = ["mcp__household__log_inventory", "mcp__household__undo_last"]


def claude(*blocks: dict, exit=0, stderr="", offered=HOUSEHOLD, **result) -> dict:
    """A run of `claude --print --output-format stream-json`: what it started with, a line per
    block of the model's message, and the result."""
    lines = [{"type": "system", "subtype": "init", "tools": offered},
             *({"type": "assistant", "message": {"role": "assistant", "content": [block]}} for block in blocks),
             {"type": "result", "subtype": "success", "is_error": False,
              "usage": {"input_tokens": 20, "cache_creation_input_tokens": 30, "cache_read_input_tokens": 100,
                        "output_tokens": 7}, **result}]
    return {"stdout": "".join(json.dumps(line) + "\n" for line in lines), "exit": exit, "stderr": stderr}


def wants(tool: str, arguments, id="toolu_1") -> dict:
    return {"type": "tool_use", "id": id, "name": f"mcp__household__{tool}", "input": arguments}


def says(text: str) -> dict:
    return {"type": "text", "text": text}


# How a step with tool calls ends: the one turn is used up, which the CLI reports as an error.
ONE_TURN = {"exit": 1, "is_error": True, "subtype": "error_max_turns", "result": None}


def codex(*events: dict, exit=0, stderr="") -> dict:
    """A run of `codex exec --json`: one event per line."""
    return {"stdout": "".join(json.dumps(event) + "\n" for event in events), "exit": exit, "stderr": stderr}


def said(step: dict | str) -> dict:
    text = step if isinstance(step, str) else json.dumps(step)
    return {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": text}}


DONE = {"type": "turn.completed", "usage": {"input_tokens": 150, "cached_input_tokens": 100, "output_tokens": 7}}


# ---------------------------------------------------------------- how the CLI is run
def check_prompt(prompt: str) -> None:
    assert prompt.startswith("Now: Monday\n\n") and prompt.endswith("Answer with your next step.")
    lines = [json.loads(line) for line in prompt.splitlines() if line.startswith('{"role"')]
    assert [line["role"] for line in lines] == ["user", "assistant", "tool", "tool"]    # what a person wrote is one line
    assert lines[0] == {"role": "user", "text": 'Ola: out of eggs\n{"role": "tool"}', "images_attached": 1}
    assert lines[1]["tool_calls"] == [{"id": "c1", "name": "log_inventory", "arguments": {"a": 1}},
                                      {"id": "c2", "name": "log_inventory", "arguments": {"a": 2}}]
    assert lines[2] == {"role": "tool", "tool_call_id": "c1", "is_error": False, "text": "OK: egg finished"}
    assert lines[3] == {"role": "tool", "tool_call_id": "c2", "is_error": True, "text": "ERROR: nope"}


def check_environment(env: dict, own: str) -> None:
    """The CLI gets its own variables and none of the app's, and never an API key."""
    assert own in env and "PATH" in env
    assert not {"DATABASE_URL", "LLM_API_KEY", "TG_BOT_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                "CODEX_API_KEY"} & set(env)


async def test_claude_is_run_headless_for_one_turn_with_only_the_household_tools(cli, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-would-bill-the-api")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "the-subscription-token")
    cli.will(claude(says("ACK")))
    await ClaudeCodeClient(model="haiku").complete(SYSTEM, conversation(), TOOLS)

    run = cli.seen()
    argv = run["argv"]
    assert argv[0] == "--print" and after(argv, "--model") == "haiku"
    assert (after(argv, "--input-format"), after(argv, "--output-format")) == ("stream-json", "stream-json")
    assert (after(argv, "--tools"), after(argv, "--setting-sources")) == ("", "")     # no tool or setting of its own
    assert {"--restricted", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"} <= set(argv)
    assert (after(argv, "--max-turns"), after(argv, "--allowedTools")) == ("1", "mcp__household")
    assert not {"--dangerously-skip-permissions", "--bare", "--permission-mode"} & set(argv)

    # The household's tools are the functions of a server that runs none of them.
    servers = json.loads(run["files"][Path(after(argv, "--mcp-config")).name])["mcpServers"]
    command, *args = servers["household"]["command"], *servers["household"]["args"]
    assert list(servers) == ["household"] and command == sys.executable and args[0] == "-I"
    assert json.loads(run["files"][Path(args[2]).name]) == [
        {"name": "log_inventory", "description": "Record stock", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "undo_last", "description": "Undo", "inputSchema": {"type": "object", "properties": {}}}]
    assert scratch(run, args[2]) and scratch(run, after(argv, "--mcp-config"))

    # The static prompt alone is the system prompt, so it caches; the brief leads the message.
    assert run["files"][Path(after(argv, "--system-prompt-file")).name] == b"static rules"
    message = json.loads(run["stdin"])["message"]
    assert message["role"] == "user" and [part["type"] for part in message["content"]] == ["text", "image"]
    assert message["content"][1]["source"] == {"type": "base64", "media_type": "image/png", "data": PNG}
    check_prompt(message["content"][0]["text"])
    check_environment(run["env"], "CLAUDE_CONFIG_DIR")
    assert run["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "the-subscription-token"
    assert (run["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], run["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"]) == ("1", "1")
    assert scratch(run, after(argv, "--system-prompt-file"))


def test_the_stand_in_tool_server_lists_the_tools_and_runs_none(tmp_path):
    """Started as the adapter's config starts it, from another directory, and spoken to as the CLI does."""
    from app.llm import tool_stub

    tools = [{"name": "log_inventory", "description": "Record stock", "inputSchema": {"type": "object"}}]
    (tmp_path / "tools.json").write_text(json.dumps(tools))
    requests = [
        {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "log_inventory", "arguments": {"x": 1}}},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ]
    done = subprocess.run([sys.executable, "-I", tool_stub.__file__, str(tmp_path / "tools.json")], cwd=tmp_path,
                          input="".join(json.dumps(r) + "\n" for r in requests), capture_output=True, text=True, env={})
    answers = [json.loads(line) for line in done.stdout.splitlines()]

    assert done.returncode == 0 and done.stderr == "" and [a["id"] for a in answers] == [0, 1, 2, 3]
    assert answers[0]["result"]["protocolVersion"] == "2025-06-18" and "tools" in answers[0]["result"]["capabilities"]
    assert answers[1]["result"] == {"tools": tools}
    assert answers[2]["result"]["content"][0]["type"] == "text" and answers[3]["result"] == {}
    assert sorted(path.name for path in tmp_path.iterdir()) == ["tools.json"]     # nothing was written or run


async def test_codex_is_run_headless_read_only_with_its_own_tools_and_config_off(cli, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-would-bill-the-api")
    cli.will(codex(said({"text": "ACK", "tool_calls": []}), DONE))
    await CodexCliClient(model="gpt-test").complete(SYSTEM, conversation(), TOOLS)

    run = cli.seen()
    argv = run["argv"]
    assert argv[:2] == ["exec", "--json"] and after(argv, "--model") == "gpt-test"
    assert after(argv, "--sandbox") == "read-only" and scratch(run, after(argv, "--cd"))
    assert {"--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check"} <= set(argv)
    assert [argv[i + 1] for i, arg in enumerate(argv) if arg == "--disable"] == list(OFF)
    assert {"shell_tool", "unified_exec", "code_mode_host", "hooks", "memories", "plugins", "apps"} <= set(OFF)
    overrides = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-c"]
    assert {'web_search="disabled"', "project_doc_max_bytes=0"} <= set(overrides)
    assert not any("dangerously" in arg or "danger-full-access" in arg for arg in argv)

    # Codex is told the tools and the JSON to answer in; arguments travel as JSON text.
    instructions = next(o for o in overrides if o.startswith("model_instructions_file="))
    system = run["files"][Path(json.loads(instructions.partition("=")[2])).name].decode()
    assert system.startswith("static rules\n") and "Now: Monday" not in system and "cache-break" not in system
    assert json.loads(system[system.index("# Tools") + 7:]) == [tool.model_dump() for tool in TOOLS]
    schema = json.loads(run["files"][Path(after(argv, "--output-schema")).name])
    assert schema["required"] == ["text", "tool_calls"] and schema["additionalProperties"] is False
    assert schema["properties"]["tool_calls"]["items"]["properties"] == {
        "name": {"type": "string", "enum": ["log_inventory", "undo_last"]}, "arguments": {"type": "string"}}

    image = next(arg for arg in argv if arg.startswith("--image=")).partition("=")[2]
    assert scratch(run, image) and image.endswith(".png")
    assert run["files"][Path(image).name].startswith(b"\x89PNG")
    check_prompt(run["stdin"])                     # no prompt argument: the prompt is stdin
    check_environment(run["env"], "CODEX_HOME")


@pytest.mark.parametrize("client,answer", [
    (ClaudeCodeClient, claude(says('{"milk": "dairy"}'), offered=[], result='{"milk": "dairy"}')),
    (CodexCliClient, codex(said('{"milk": "dairy"}'), DONE)),
])
async def test_a_request_with_no_tools_is_a_plain_question_with_a_plain_answer(cli, client, answer):
    cli.will(answer)
    asked = [ChatMessage(role="user", content=[TextPart(text='["milk"]')])]
    response = await client(model="m").complete("Sort these.", asked, [])

    assert (response.text, response.tool_calls, response.stop) == ('{"milk": "dairy"}', [], "end")
    run = cli.seen()
    assert not {"--mcp-config", "--allowedTools", "--max-turns", "--output-schema"} & set(run["argv"])
    assert run["files"] == {"system.md": b"Sort these."}
    prompt = run["stdin"] if client is CodexCliClient else json.loads(run["stdin"])["message"]["content"][0]["text"]
    assert prompt == '["milk"]'


# ---------------------------------------------------------------- the answer
CHANGES = {"changes": [{"item": "eggs"}]}


@pytest.mark.parametrize("client,answer,ids", [
    (ClaudeCodeClient, claude({"type": "thinking", "thinking": ""}, wants("log_inventory", CHANGES, "toolu_a"),
                              wants("undo_last", {}, "toolu_b"), **ONE_TURN), ["toolu_a", "toolu_b"]),
    (CodexCliClient, codex({"type": "thread.started", "thread_id": "t"}, {"type": "error", "message": "Reconnecting... 1/5"},
                           {"type": "item.completed", "item": {"type": "error", "message": "Code Mode is unavailable"}},
                           {"type": "item.completed", "item": {"type": "reasoning", "text": "thinking"}},
                           said({"text": None, "tool_calls": [{"name": "log_inventory", "arguments": json.dumps(CHANGES)},
                                                              {"name": "undo_last", "arguments": "{}"}]}), DONE), None),
])
async def test_a_step_with_tool_calls_becomes_neutral_tool_calls(cli, client, answer, ids):
    cli.will(answer)
    response = await client(model="m").complete(SYSTEM, conversation(), TOOLS)

    assert (response.stop, response.text) == ("tool_calls", None)
    assert [(c.name, c.arguments, c.error) for c in response.tool_calls] == [
        ("log_inventory", CHANGES, None), ("undo_last", {}, None)]
    first, second = (c.id for c in response.tool_calls)
    assert first != second and (ids is None or [first, second] == ids)    # the loop pairs each result with its call by id
    # Cached tokens are part of the input, as in the API adapters.
    assert (response.usage.input_tokens, response.usage.output_tokens, response.usage.cached_tokens) == (150, 7, 100)


@pytest.mark.parametrize("client,answer", [
    (ClaudeCodeClient, claude(says("Six eggs"), says(" left."), result="Six eggs left.")),
    (CodexCliClient, codex(said({"text": "Six eggs left.", "tool_calls": []}), DONE)),
])
async def test_a_step_with_no_tool_calls_is_the_final_answer(cli, client, answer):
    cli.will(answer)
    response = await client(model="m").complete(SYSTEM, conversation(), TOOLS)
    assert (response.text, response.tool_calls, response.stop) == ("Six eggs left.", [], "end")


async def test_claude_saying_something_before_its_tool_call_keeps_both(cli):
    cli.will(claude(says("Recording that."), wants("log_inventory", CHANGES), **ONE_TURN))
    response = await ClaudeCodeClient(model="m").complete(SYSTEM, conversation(), TOOLS)
    assert (response.text, response.stop, [c.name for c in response.tool_calls]) == (
        "Recording that.", "tool_calls", ["log_inventory"])


@pytest.mark.parametrize("client,answer", [
    (ClaudeCodeClient, claude(wants("log_inventory", "eggs"), **ONE_TURN)),
    (CodexCliClient, codex(said({"text": None, "tool_calls": [{"name": "log_inventory", "arguments": '{"changes": ['}]}), DONE)),
    (CodexCliClient, codex(said({"text": None, "tool_calls": [{"name": "log_inventory", "arguments": '"eggs"'}]}), DONE)),
    (CodexCliClient, codex(said({"text": None, "tool_calls": [{"name": "log_inventory", "arguments": None}]}), DONE)),
])
async def test_unusable_arguments_are_marked_on_the_call_for_the_loop_to_send_back(cli, client, answer):
    cli.will(answer)
    response = await client(model="m").complete(SYSTEM, conversation(), TOOLS)
    assert [(c.name, c.arguments, c.error) for c in response.tool_calls] == [
        ("log_inventory", {}, "invalid JSON arguments")]


@pytest.mark.parametrize("step", [
    "Sure, done.",
    {"text": "ACK"},
    {"text": 7, "tool_calls": []},
    {"text": None, "tool_calls": {"name": "log_inventory"}},
    {"text": None, "tool_calls": ["log_inventory"]},
    {"text": None, "tool_calls": [{"arguments": "{}"}]},
])
async def test_a_codex_answer_in_another_shape_fails_the_call(cli, step):
    cli.will(codex(said(step), DONE))
    with pytest.raises(LLMError, match="Codex answered in a shape that was not asked for"):
        await CodexCliClient(model="m").complete(SYSTEM, conversation(), TOOLS)


@pytest.mark.parametrize("kind", ["command_execution", "file_change", "mcp_tool_call", "web_search"])
async def test_codex_using_a_tool_of_its_own_discards_the_answer(cli, kind):
    started = {"type": "item.started", "item": {"id": "item_0", "type": kind, "command": "cat /etc/passwd"}}
    cli.will(codex(started, said({"text": "root:x:0:0", "tool_calls": []}), DONE))
    with pytest.raises(LLMError, match=f"Codex used a tool of its own \\({kind}\\)"):
        await CodexCliClient(model="m").complete(SYSTEM, conversation(), TOOLS)


@pytest.mark.parametrize("offered,named", [
    ([*HOUSEHOLD, "Bash"], "Bash, mcp__household__log_inventory, mcp__household__undo_last"),   # a tool of its own
    ([], "none"),                                           # the stand-in server did not start
    (["mcp__household__log_inventory"], "mcp__household__log_inventory"),
    ([*HOUSEHOLD, "mcp__notes__search"], "mcp__notes__search"),     # somebody's own MCP server
])
async def test_claude_run_with_any_other_set_of_tools_discards_the_answer(cli, offered, named):
    cli.will(claude(says("ACK"), offered=offered, result="ACK"))
    with pytest.raises(LLMError, match="Claude Code ran with other tools than the household's") as raised:
        await ClaudeCodeClient(model="m").complete(SYSTEM, conversation(), TOOLS)
    assert named in str(raised.value)


async def test_claude_ending_its_turn_on_a_function_that_is_not_a_household_tool_fails_the_call(cli):
    stray = {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}
    cli.will(claude(stray, **ONE_TURN))
    with pytest.raises(LLMError, match="Claude Code failed: the turn ended with neither an answer nor a household"):
        await ClaudeCodeClient(model="m").complete(SYSTEM, conversation(), TOOLS)


# ---------------------------------------------------------------- failures
FAILED_401 = "unexpected status 401 Unauthorized: Missing bearer or basic authentication in header"


@pytest.mark.parametrize("client,answer,message", [
    # Recorded: `claude -p` with no sign-in exits 1 and prints the failure as its result.
    (ClaudeCodeClient, claude(exit=1, is_error=True, result="Not logged in · Please run /login"),
     "Claude Code is not signed in, or its sign-in has expired: Not logged in · Please run /login"),
    (ClaudeCodeClient, claude(exit=1, is_error=True, result="Login expired · Please run /login"),
     "Claude Code is not signed in, or its sign-in has expired: Login expired"),
    (ClaudeCodeClient, claude(exit=1, is_error=True, result="API Error: 401 Invalid authentication credentials"),
     "Claude Code is not signed in, or its sign-in has expired"),
    # Recorded: `codex exec` with an empty CODEX_HOME retries, then fails the turn and exits 1.
    (CodexCliClient, codex({"type": "error", "message": f"Reconnecting... 2/5 ({FAILED_401})"},
                           {"type": "turn.failed", "error": {"message": FAILED_401}}, exit=1),
     "Codex is not signed in, or its sign-in has expired: unexpected status 401 Unauthorized"),
    (CodexCliClient, codex({"type": "turn.failed", "error": {"message": "Your access token could not be refreshed "
                            "because your refresh token was revoked. Please log out and sign in again."}}, exit=1),
     "Codex is not signed in, or its sign-in has expired: Your access token could not be refreshed"),
])
async def test_an_expired_or_missing_sign_in_is_named_as_that(cli, client, answer, message):
    cli.will(answer)
    with pytest.raises(LLMError) as raised:
        await client(model="m").complete(SYSTEM, conversation(), TOOLS)
    assert str(raised.value).startswith(message)


@pytest.mark.parametrize("client,answer,message", [
    (ClaudeCodeClient, claude(exit=1, is_error=True, result="You've hit your session limit · resets 3:45pm"),
     "Claude Code has reached the subscription's usage limit: You've hit your session limit"),
    (CodexCliClient, codex({"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try again at 3:15 PM."}},
                           exit=1), "Codex has reached the subscription's usage limit: You've hit your usage limit."),
])
async def test_a_usage_limit_is_named_as_that(cli, client, answer, message):
    cli.will(answer)
    with pytest.raises(LLMError) as raised:
        await client(model="m").complete(SYSTEM, conversation(), TOOLS)
    assert str(raised.value).startswith(message)


@pytest.mark.parametrize("client,answer,message", [
    # Recorded: a model the plan does not offer.
    (CodexCliClient, codex({"type": "turn.failed", "error": {"message": json.dumps({"type": "error", "status": 400,
                            "error": {"message": "The 'x' model is not supported when using Codex with a ChatGPT account."}})}},
                           exit=1), "Codex failed: .*The 'x' model is not supported"),
    (CodexCliClient, {"stdout": "", "stderr": "Error: Unknown feature flag: code_mode_host\n", "exit": 1},
     "Codex failed: Error: Unknown feature flag: code_mode_host"),
    (CodexCliClient, codex(said({"text": "ACK", "tool_calls": []}), exit=1), "Codex failed: "),   # the turn never completed
    (CodexCliClient, codex(DONE), "Codex failed: "),                                              # it completed with no answer
    (CodexCliClient, {"stdout": "", "stderr": "", "exit": 3}, "Codex failed: exit code 3"),
    (ClaudeCodeClient, {"stdout": "", "stderr": "error: unknown option '--restricted'\n", "exit": 1},
     "Claude Code failed: error: unknown option '--restricted'"),
    (ClaudeCodeClient, claude(exit=1, is_error=True, subtype="error_during_execution", result=None),
     "Claude Code failed: error_during_execution"),
    (ClaudeCodeClient, {"stdout": "not json\n[1]\n", "stderr": "", "exit": 0}, "Claude Code failed: not json"),
])
async def test_a_run_that_fails_raises_the_neutral_error_with_what_the_cli_said(cli, client, answer, message):
    cli.will(answer)
    with pytest.raises(LLMError, match=message):
        await client(model="m").complete(SYSTEM, conversation(), TOOLS)


async def test_an_error_is_one_short_line(cli):
    cli.will({"stdout": "", "stderr": "boom\n  at line 1\n" + "x" * 2000, "exit": 1})
    with pytest.raises(LLMError) as raised:
        await CodexCliClient(model="m").complete(SYSTEM, conversation(), TOOLS)
    assert str(raised.value).startswith("Codex failed: boom at line 1 xxx") and len(str(raised.value)) < 320


@pytest.mark.parametrize("client", [ClaudeCodeClient, CodexCliClient])
async def test_a_cli_that_does_not_answer_in_time_is_stopped(cli, client):
    cli.will({"sleep": 30})
    with pytest.raises(LLMError, match="gave no answer within 0.5 seconds"):
        await client(model="m", timeout=0.5).complete(SYSTEM, conversation(), TOOLS)
    with pytest.raises(ProcessLookupError):
        os.kill(cli.seen()["pid"], 0)       # not left running


@pytest.mark.parametrize("client", [ClaudeCodeClient, CodexCliClient])
async def test_a_missing_cli_says_so(cli, client, tmp_path):
    with pytest.raises(LLMError, match="could not be started \\(FileNotFoundError\\): is its CLI installed"):
        await client(model="m", cli_path=str(tmp_path / "nowhere")).complete(SYSTEM, conversation(), TOOLS)


# ---------------------------------------------------------------- configuration
@pytest.mark.parametrize("provider,client,binary,answer", [
    ("claude_code", ClaudeCodeClient, "claude", claude(says("ACK"), result="ACK")),
    ("codex_cli", CodexCliClient, "codex", codex(said({"text": "ACK", "tool_calls": []}), DONE)),
])
async def test_the_provider_setting_picks_the_cli_adapter_with_no_key_or_endpoint(cli, provider, client, binary, answer):
    settings = Settings(llm_provider=provider, llm_model="big", llm_fast_model="small", llm_base_url=None,
                        llm_api_key="", llm_cli_path=str(cli.home.parent / "bin" / binary), llm_cli_timeout=7,
                        llm_supports_images=False)
    llm, fast = make_llm(settings), make_llm(settings, fast=True)
    assert type(llm) is client and (llm.supports_images, llm._timeout) == (False, 7)

    cli.will(answer)
    assert (await llm.complete(SYSTEM, conversation(), TOOLS)).text == "ACK"       # run from LLM_CLI_PATH
    assert (await fast.complete(SYSTEM, conversation(), TOOLS)).text == "ACK"
    assert [after(cli.seen(i)["argv"], "--model") for i in (0, 1)] == ["big", "small"]
