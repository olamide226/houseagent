"""Claude through the Claude Code CLI and its own sign-in, a Claude subscription (ADR 0032)."""
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from app.llm import tool_stub
from app.llm.cli import failure, render, run_cli
from app.llm.types import CACHE_BREAK, ChatMessage, LLMError, LLMResponse, ToolCall, ToolDef, Usage

NAME = "Claude Code"
# Print mode with nothing of the machine's own: no built-in tools, no settings files (and so no
# CLAUDE.md, hooks or plugins), no skills, no MCP server but ours, no session left on disk.
ISOLATED = ["--tools", "", "--restricted", "--setting-sources", "", "--strict-mcp-config",
            "--disable-slash-commands", "--no-session-persistence"]
ENV = ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "NODE_EXTRA_CA_CERTS")
FIXED = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}   # and no self-update
# The household's tools reach the model as the functions of a stand-in MCP server that runs
# nothing. One turn: the run ends as soon as the model has said which of them to call.
SERVER = "household"
PREFIX = f"mcp__{SERVER}__"


def _events(stdout: str) -> list[dict[str, Any]]:
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


class ClaudeCodeClient:
    def __init__(self, *, model: str, cli_path: str | None = None, timeout: float = 120,
                 supports_images: bool = True) -> None:
        self.supports_images = supports_images
        self._model = model
        self._cli = cli_path or "claude"
        self._timeout = timeout

    def _command(self, scratch: str, system: str, tools: list[ToolDef]) -> list[str]:
        prompt_file = Path(scratch, "system.md")
        prompt_file.write_text(system)
        argv = [self._cli, "--print", "--model", self._model, "--system-prompt-file", str(prompt_file),
                "--input-format", "stream-json", "--output-format", "stream-json", "--verbose", *ISOLATED]
        if tools:
            tools_file, servers_file = Path(scratch, "tools.json"), Path(scratch, "mcp.json")
            tools_file.write_text(json.dumps(
                [{"name": t.name, "description": t.description, "inputSchema": t.parameters} for t in tools]))
            # -I: as a script it would otherwise find this package's types.py before the standard one.
            servers_file.write_text(json.dumps({"mcpServers": {SERVER: {
                "command": sys.executable, "args": ["-I", str(tool_stub.__file__), str(tools_file)]}}}))
            argv += ["--mcp-config", str(servers_file), "--allowedTools", f"mcp__{SERVER}", "--max-turns", "1"]
        return argv

    async def complete(self, system: str, messages: list[ChatMessage], tools: list[ToolDef],
                       max_tokens: int = 1024, temperature: float = 0.2) -> LLMResponse:
        # `max_tokens` and `temperature` have no flag in the CLI. The static prompt is the system
        # prompt, which the CLI caches with the tools; the brief changes each turn, so it leads
        # the message instead.
        static, _, brief = system.partition(CACHE_BREAK)
        prompt, images = render(brief, messages, tools)
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content += [{"type": "image", "source": {"type": "base64", "media_type": p.mime, "data": p.data_b64}}
                    for p in images]
        with tempfile.TemporaryDirectory(prefix="houseagent-llm-") as scratch:
            code, out, err = await run_cli(
                NAME, self._command(scratch, static, tools),
                stdin=json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n",
                cwd=scratch, seconds=self._timeout, env=ENV, fixed=FIXED)

        events = _events(out)
        result = next((e for e in reversed(events) if e.get("type") == "result"), None)
        if result is None:
            raise failure(NAME, err or out or f"exit code {code}")
        # A failure inside the run, a missing sign-in included, is the result. Reaching the one
        # turn is not one: it is how a step with tool calls ends.
        if result.get("is_error") and result.get("subtype") != "error_max_turns":
            raise failure(NAME, str(result.get("result") or result.get("subtype")))
        started = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
        offered = started.get("tools") or []
        if set(offered) != {PREFIX + t.name for t in tools}:
            raise LLMError(f"{NAME} ran with other tools than the household's ({', '.join(sorted(offered)) or 'none'})"
                           "; its answer was discarded")

        blocks = [block for e in events if e.get("type") == "assistant"
                  for block in (e.get("message") or {}).get("content") or [] if isinstance(block, dict)]
        calls = [ToolCall(id=str(b.get("id")), name=str(b.get("name")).removeprefix(PREFIX),
                          arguments=b["input"] if isinstance(b.get("input"), dict) else {},
                          error=None if isinstance(b.get("input"), dict) else "invalid JSON arguments")
                 for b in blocks if b.get("type") == "tool_use" and str(b.get("name")).startswith(PREFIX)]
        if result.get("is_error") and not calls:
            raise failure(NAME, "the turn ended with neither an answer nor a household tool call")
        text = "".join(b.get("text") or "" for b in blocks if b.get("type") == "text")
        spent = result.get("usage") or {}
        cached, written = spent.get("cache_read_input_tokens") or 0, spent.get("cache_creation_input_tokens") or 0
        return LLMResponse(
            text=text or None, tool_calls=calls, stop="tool_calls" if calls else "end",
            usage=Usage(input_tokens=(spent.get("input_tokens") or 0) + cached + written,
                        output_tokens=spent.get("output_tokens") or 0, cached_tokens=cached))
