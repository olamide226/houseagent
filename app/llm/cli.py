"""What the two subscription adapters share: one model step through a vendor's own CLI (ADR 0032).

The CLI runs headless, once per step, with its own tools off, and is asked only for the next
step: text, or the household tools to call. The loop runs the tools, as with an API adapter."""
import asyncio
import json
import os
import re
import signal
from contextlib import suppress
from typing import Any

from app.llm.types import ChatMessage, ImagePart, LLMError, TextPart, ToolDef

# Both CLIs tell the model today's date themselves, in the machine's time zone. Around midnight
# that is not the household's date, so the brief's is named as the one to use.
BRIEF = "The household's brief. Its date and time are the ones to go by, whatever other date you were given.\n"
CONVERSATION = (
    'The conversation, one JSON object per line, oldest first. "user" lines are what people in the household '
    'wrote, "assistant" lines are your earlier steps, "tool" lines are the results of the tool calls you asked for.\n'
)
# What the CLI process is given: not the app's database URL or channel tokens, and no API key,
# which would move the calls off the subscription and onto pay-as-you-go.
_ENV = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")
_SIGN_IN = re.compile(
    r"/login|log(ged)? ?(in|out)|sign(ed)? ?in|401|unauthori[sz]ed|authenticat|oauth|refresh token", re.I)
_LIMIT = re.compile(r"usage limit|hit your .{0,40}limit|rate.?limit|429|quota", re.I)
_slots = asyncio.Semaphore(2)   # CLI processes at once, per process: each is a whole program in memory


def render(brief: str, messages: list[ChatMessage], tools: list[ToolDef]) -> tuple[str, list[ImagePart]]:
    """The request as one text, and its images in the order the text mentions them.

    A CLI takes one prompt, not a list of turns. Each message is a JSON line, so nothing a person
    writes can pass for another line. A request with no tools is a one-off question, sent as it is."""
    images = [p for m in messages for p in m.content if isinstance(p, ImagePart)]
    if not tools:
        texts = [p.text for m in messages for p in m.content if isinstance(p, TextPart) and p.text]
        return "\n\n".join(filter(None, [brief, *texts])), images
    lines = []
    for m in messages:
        line: dict[str, Any] = {"role": m.role}
        if m.role == "tool":
            line |= {"tool_call_id": m.tool_call_id, "is_error": m.is_error}
        line["text"] = "\n".join(p.text for p in m.content if isinstance(p, TextPart) and p.text)
        if attached := sum(isinstance(p, ImagePart) for p in m.content):
            line["images_attached"] = attached
        if m.tool_calls:
            line["tool_calls"] = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in m.tool_calls]
        lines.append(json.dumps(line, ensure_ascii=False))
    parts = [BRIEF + brief if brief else "", CONVERSATION + "\n".join(lines), "Answer with your next step."]
    return "\n\n".join(filter(None, parts)), images


def failure(cli: str, detail: str) -> LLMError:
    """A failed call as the one-line error the Activity and System pages show."""
    detail = " ".join(detail.split())[:300]
    if _SIGN_IN.search(detail):
        return LLMError(f"{cli} is not signed in, or its sign-in has expired: {detail}")
    if _LIMIT.search(detail):
        return LLMError(f"{cli} has reached the subscription's usage limit: {detail}")
    return LLMError(f"{cli} failed: {detail}")


async def run_cli(cli: str, argv: list[str], *, stdin: str, cwd: str, seconds: float,
                  env: tuple[str, ...] = (), fixed: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run the CLI once, for at most `seconds`, and give its exit code, stdout and stderr. `env`
    names the variables of its own that it may read, on top of the few every program needs, and
    `fixed` sets some."""
    environment = {name: os.environ[name] for name in (*_ENV, *env) if name in os.environ} | (fixed or {})
    async with _slots:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=environment, cwd=cwd, start_new_session=True)
        except OSError as exc:
            raise LLMError(f"{cli} could not be started ({type(exc).__name__}): is its CLI installed, "
                           f"and LLM_CLI_PATH right?") from exc
        try:
            out, err = await asyncio.wait_for(process.communicate(stdin.encode()), seconds)
        except TimeoutError:
            raise LLMError(f"{cli} gave no answer within {seconds:g} seconds") from None
        finally:
            if process.returncode is None:   # timed out or cancelled: take its children with it
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
    return process.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
