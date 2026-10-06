"""LettaRuntime: the optional runtime behind `AGENT_RUNTIME=letta` (spec section 8.4).

One Letta agent per household. Letta keeps the conversation and runs the model; Postgres stays
the source of truth, so the two memory blocks are rewritten from it before every turn and every
tool is a thin function that calls back into `/internal/tools/{name}` (app/agent/internal.py).

Written against the Letta REST API as `letta-client` 1.12 wraps it, and run against a
self-hosted server at 0.16.8. See docs/agent-and-tools.md and ADR 0029.
"""
from typing import Any

import structlog

from app.agent.base import AgentResult, Ctx
from app.agent.internal import turn_in_flight
from app.agent.loop import LOST, final_result, load_photos, turn_text
from app.agent.prompt import build_brief, system_prompt
from app.agent.tools import REGISTRY, ToolSpec
from app.core.envelope import Envelope
from app.db import execute, fetch_val
from app.llm.types import CACHE_BREAK, LLMError, Usage
from app.media.store import MediaStore
from app.services import households

log = structlog.get_logger()
BLOCK_LIMIT = 20000   # characters; the brief is capped near 1,500 tokens
TIMEOUT_SECONDS = 180
# What every tool reads from the agent's tool environment. Set per turn, so a new token or a
# turn run by the other process (the Playground runs in the api) takes effect at once.
ENV_HOUSEHOLD, ENV_URL, ENV_TOKEN = "HOUSEAGENT_HOUSEHOLD_ID", "HOUSEAGENT_TOOL_URL", "HOUSEAGENT_TOOL_TOKEN"


def tool_source(spec: ToolSpec) -> str:
    """A Letta custom tool: the same name and arguments, and a body that only posts them to us."""
    schema = spec.args.model_json_schema()
    required = [name for name in schema["properties"] if name in schema.get("required", [])]
    optional = [name for name in schema["properties"] if name not in required]
    signature = ", ".join([*required, *(f"{name}=None" for name in optional)])
    given = ", ".join(f'"{name}": {name}' for name in [*required, *optional])
    return f'''def {spec.name}({signature}):
    """Calls the household service. See the tool description."""
    import json
    import os
    import urllib.request

    args = {{name: value for name, value in {{{given}}}.items() if value is not None}}
    request = urllib.request.Request(
        os.environ["{ENV_URL}"] + "/internal/tools/{spec.name}",
        data=json.dumps({{"household_id": os.environ["{ENV_HOUSEHOLD}"], "args": args}}).encode(),
        headers={{"Authorization": "Bearer " + os.environ["{ENV_TOKEN}"], "Content-Type": "application/json"}})
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)["result"]
'''


def tool_schema(spec: ToolSpec) -> dict[str, Any]:
    schema = spec.args.model_json_schema()
    return {"name": spec.name, "description": (spec.fn.__doc__ or "").strip(),
            "parameters": _plain(schema, schema.get("$defs", {}))}


def _plain(node: Any, defs: dict[str, Any]) -> Any:
    """The same JSON schema in the plainer form Letta's argument handling needs: references
    inlined, and an optional value given its one type instead of `anyOf` that type or null.
    Letta 0.16.8 fails a call ("Unsupported type: None") when such an argument is passed."""
    if isinstance(node, list):
        return [_plain(item, defs) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        referred = defs[node["$ref"].rsplit("/", 1)[1]]
        return _plain({**referred, **{key: value for key, value in node.items() if key != "$ref"}}, defs)
    node = {key: _plain(value, defs) for key, value in node.items() if key != "$defs"}
    kinds = [option for option in node.get("anyOf", []) if option != {"type": "null"}]
    if len(kinds) == 1:
        node = {**kinds[0], **{key: value for key, value in node.items() if key != "anyOf"}}
    return node


class LettaRuntime:
    def __init__(self, client: Any, *, tool_url: str, tool_token: str, model: str | None = None,
                 media: MediaStore | None = None, agent_name: str = "Home", max_iterations: int = 8,
                 supports_images: bool = True) -> None:
        self._client = client          # letta_client.AsyncLetta
        self._tool_url = tool_url.rstrip("/")
        self._tool_token = tool_token
        self._model = model
        self._media = media
        self._agent_name = agent_name
        self._max_iterations = max_iterations
        self._supports_images = supports_images
        self._tool_ids: list[str] | None = None

    async def handle(self, env: Envelope, ctx: Ctx) -> AgentResult:
        onboarding = await households.onboarding(ctx.conn, env.household_id)
        prompt = system_prompt(self._agent_name, await build_brief(ctx.conn, env, env.received_at), onboarding)
        persona, household = prompt.split(CACHE_BREAK)
        photos, notes = await load_photos(env, self._media, self._supports_images)
        content: list[dict[str, Any]] = [{"type": "text", "text": turn_text(env, notes)}]
        content += [{"type": "image", "source": {"type": "base64", "media_type": photo.mime, "data": photo.data_b64}}
                    for photo in photos]
        secrets = {ENV_HOUSEHOLD: env.household_id, ENV_URL: self._tool_url, ENV_TOKEN: self._tool_token}
        try:
            agent_id = await self._agent(ctx, {"persona": persona, "household": household}, secrets)
            with turn_in_flight(ctx, onboarding["step"] is not None) as turn:
                response = await self._client.agents.messages.create(
                    agent_id, messages=[{"role": "user", "content": content}], max_steps=self._max_iterations,
                    timeout=TIMEOUT_SECONDS)
        except LLMError:
            raise
        except Exception as exc:
            # Never the exception text: a Letta error can quote the request, which holds the tool token.
            raise LLMError(f"letta: {type(exc).__name__}") from None
        said = [_text(message.content) for message in response.messages
                if getattr(message, "message_type", None) == "assistant_message"]
        usage = Usage(input_tokens=response.usage.prompt_tokens or 0,
                      output_tokens=response.usage.completion_tokens or 0,
                      cached_tokens=getattr(response.usage, "cached_input_tokens", None) or 0)
        if not said and response.stop_reason.stop_reason == "max_steps":
            return AgentResult(reply=LOST, tool_calls=turn.records, usage=usage)
        return final_result(said[-1] if said else None, turn.records, usage)

    async def _agent(self, ctx: Ctx, blocks: dict[str, str], secrets: dict[str, str]) -> str:
        """The household's agent, created on its first turn; its memory blocks and tool
        environment are rewritten before every turn."""
        agent_id: str | None = await fetch_val(
            ctx.conn, "select letta_agent_id from households where id = :h", h=ctx.household_id)
        if agent_id is None:
            agent = await self._client.agents.create(
                name=f"household-{ctx.household_id}", model=self._model, include_base_tools=False,
                tool_ids=await self._tools(), secrets=secrets,
                memory_blocks=[{"label": label, "value": value, "limit": BLOCK_LIMIT}
                               for label, value in blocks.items()])
            await execute(ctx.conn, "update households set letta_agent_id = :a where id = :h",
                          a=agent.id, h=ctx.household_id)
            log.info("letta_agent_created", household_id=ctx.household_id)
            return str(agent.id)
        await self._tools()
        for label, value in blocks.items():
            await self._client.agents.blocks.update(label, agent_id=agent_id, value=value)
        await self._client.agents.update(agent_id, secrets=secrets)
        return agent_id

    async def _tools(self) -> list[str]:
        """Register the twelve tools once per process. Upsert is by name, so a deploy that
        changes a tool changes it for every existing agent."""
        if self._tool_ids is None:
            self._tool_ids = [
                str((await self._client.tools.upsert(source_code=tool_source(spec), json_schema=tool_schema(spec))).id)
                for spec in REGISTRY.values()]
        return self._tool_ids


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = [part.get("text") if isinstance(part, dict) else getattr(part, "text", None) for part in content or []]
    return "".join(part or "" for part in parts)
