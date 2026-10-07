"""OpenAI models through the Codex CLI and its own sign-in, a ChatGPT plan (ADR 0032)."""
import base64
import json
import mimetypes
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.llm.cli import failure, render, run_cli
from app.llm.types import CACHE_BREAK, ChatMessage, ImagePart, LLMError, LLMResponse, ToolCall, ToolDef, Usage

NAME = "Codex"
# Codex is an agent with a shell. These switch off the tools that run or read anything, and the
# parts of a personal setup that would otherwise join the turn. `--ignore-user-config` leaves
# out config.toml, and with it the user's own MCP servers. Skills have no switch: a budget of
# one token (zero is refused) leaves their list, and its tokens, out of every request.
OFF = ("shell_tool", "unified_exec", "code_mode_host", "view_image", "image_generation", "browser_use",
       "computer_use", "multi_agent", "hooks", "memories", "plugins", "apps")
ISOLATED = ["--ephemeral", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
            "--sandbox", "read-only", "--color", "never",
            "-c", "project_doc_max_bytes=0", "-c", 'web_search="disabled"', "-c", "skills.max_context_tokens=1",
            *(arg for feature in OFF for arg in ("--disable", feature))]
ENV = ("CODEX_HOME", "CODEX_CA_CERTIFICATE")
# Every other item in the event stream is Codex doing something of its own: a command, a file
# change, an MCP or web call. An `error` item is a warning; the turn's failure is its own event.
QUIET = {"agent_message", "reasoning", "error"}
# Codex has no way to be handed functions that it does not run itself, so the step comes back
# as JSON in the shape of `--output-schema`. That schema allows no free-form object, so a
# tool's arguments travel as JSON text.
PROTOCOL = (
    "\n\n# How to answer\n\n"
    "You run nothing yourself here: no shell, no files, no web. Answer with one JSON object and nothing else: "
    '{"text": ..., "tool_calls": [...]}\n'
    '- "tool_calls": the tools to call now, in order. Each is {"name": ..., "arguments": ...} where "arguments" is '
    """a JSON object, written out as a string, that fits the tool's "parameters" schema below. Their results come """
    'back as "tool" lines in the conversation, and you are asked again.\n'
    '- "text": what you say. With tool calls it is normally null. With no tool calls it is your whole reply for '
    "this turn (or exactly ACK or NOOP) and the turn ends.\n\n"
    "# Tools\n\n"
)


def _schema(tools: list[ToolDef]) -> dict[str, Any]:
    call = {"type": "object", "additionalProperties": False, "required": ["name", "arguments"],
            "properties": {"name": {"type": "string", "enum": [t.name for t in tools]},
                           "arguments": {"type": "string"}}}
    return {"type": "object", "additionalProperties": False, "required": ["text", "tool_calls"],
            "properties": {"text": {"type": ["string", "null"]}, "tool_calls": {"type": "array", "items": call}}}


def _step(answer: str) -> tuple[str | None, list[ToolCall]]:
    try:
        step = json.loads(answer)
        text, raw_calls = step["text"], step["tool_calls"]
        if not (text is None or isinstance(text, str)) or not isinstance(raw_calls, list):
            raise TypeError
        calls = []
        for raw in raw_calls:
            arguments, error = raw["arguments"], None
            with suppress(ValueError, TypeError):
                arguments = json.loads(arguments)
            if not isinstance(arguments, dict):
                arguments, error = {}, "invalid JSON arguments"
            calls.append(ToolCall(id=f"call_{uuid4().hex[:12]}", name=str(raw["name"]), arguments=arguments,
                                  error=error))
    except (ValueError, KeyError, TypeError) as exc:
        raise LLMError(f"{NAME} answered in a shape that was not asked for") from exc
    return text or None, calls


class CodexCliClient:
    def __init__(self, *, model: str, cli_path: str | None = None, timeout: float = 120,
                 supports_images: bool = True) -> None:
        self.supports_images = supports_images
        self._model = model
        self._cli = cli_path or "codex"
        self._timeout = timeout

    def _command(self, scratch: str, system: str, tools: list[ToolDef], images: list[ImagePart]) -> list[str]:
        prompt_file = Path(scratch, "system.md")
        prompt_file.write_text(system)
        argv = [self._cli, "exec", "--json", "--model", self._model, "--cd", scratch, *ISOLATED,
                "-c", f"model_instructions_file={json.dumps(str(prompt_file))}"]
        if tools:
            schema_file = Path(scratch, "step.schema.json")
            schema_file.write_text(json.dumps(_schema(tools)))
            argv += ["--output-schema", str(schema_file)]
        for number, image in enumerate(images):
            image_file = Path(scratch, f"image-{number}{mimetypes.guess_extension(image.mime) or '.jpg'}")
            image_file.write_bytes(base64.b64decode(image.data_b64))
            argv.append(f"--image={image_file}")
        return argv   # no prompt argument: Codex then reads the whole prompt from stdin

    async def complete(self, system: str, messages: list[ChatMessage], tools: list[ToolDef],
                       max_tokens: int = 1024, temperature: float = 0.2) -> LLMResponse:
        # `max_tokens` and `temperature` have no flag in the CLI.
        static, _, brief = system.partition(CACHE_BREAK)
        prompt, images = render(brief, messages, tools)
        with tempfile.TemporaryDirectory(prefix="houseagent-llm-") as scratch:
            if tools:
                static += PROTOCOL + json.dumps([t.model_dump() for t in tools], ensure_ascii=False)
            code, out, err = await run_cli(NAME, self._command(scratch, static, tools, images), stdin=prompt,
                                           cwd=scratch, seconds=self._timeout, env=ENV)

        answer, usage, said = None, None, ""
        for line in out.splitlines():
            try:
                event = json.loads(line)
                kind, item = str(event["type"]), event.get("item") or {}
                made = item.get("type")
            except (ValueError, KeyError, TypeError, AttributeError):
                continue   # not an event
            if kind.startswith("item.") and made not in QUIET:
                raise LLMError(f"{NAME} used a tool of its own ({made}); its answer was discarded")
            if kind == "item.completed" and made == "agent_message":
                answer = item.get("text")
            elif kind == "turn.completed":
                usage = event.get("usage") or {}
            elif kind == "turn.failed":
                said = str((event.get("error") or {}).get("message"))
            elif kind == "error":   # also the "Reconnecting..." notices of a turn that then succeeds
                said = said or str(event.get("message"))
        if usage is None or not answer:
            raise failure(NAME, said or err or out or f"exit code {code}")
        spent = Usage(input_tokens=usage.get("input_tokens") or 0, output_tokens=usage.get("output_tokens") or 0,
                      cached_tokens=usage.get("cached_input_tokens") or 0)
        if not tools:
            return LLMResponse(text=answer, stop="end", usage=spent)
        text, calls = _step(answer)
        return LLMResponse(text=text, tool_calls=calls, stop="tool_calls" if calls else "end", usage=spent)
