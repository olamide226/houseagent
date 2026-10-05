"""Anthropic Messages adapter (also serves Anthropic-compatible endpoints via base_url)."""
from typing import Any

import anthropic

from app.llm.types import (
    CACHE_BREAK,
    ChatMessage,
    LLMError,
    LLMResponse,
    TextPart,
    ToolCall,
    ToolDef,
    Usage,
)

_STOP = {"end_turn": "end", "stop_sequence": "end", "tool_use": "tool_calls", "max_tokens": "length"}


def _blocks(m: ChatMessage) -> list[Any]:
    if m.role == "assistant" and m.opaque is not None:
        return list(m.opaque)   # the provider's own blocks (reasoning included), unchanged
    blocks: list[Any] = [
        {"type": "text", "text": p.text} if isinstance(p, TextPart)
        else {"type": "image", "source": {"type": "base64", "media_type": p.mime, "data": p.data_b64}}
        for p in m.content
        if not (isinstance(p, TextPart) and not p.text)
    ]
    if m.role == "tool":
        return [{"type": "tool_result", "tool_use_id": m.tool_call_id, "content": blocks,
                 "is_error": m.is_error}]
    blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments} for c in m.tool_calls]
    return blocks


def _messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Tool results travel as user turns; adjacent same-role turns merge; a user turn leads."""
    wire: list[dict[str, Any]] = []
    for m in messages:
        role = "assistant" if m.role == "assistant" else "user"
        blocks = _blocks(m)
        if not blocks or (not wire and role == "assistant"):
            continue
        if wire and wire[-1]["role"] == role:
            wire[-1]["content"] += blocks
        else:
            wire.append({"role": role, "content": blocks})
    return wire


def _system(system: str) -> list[dict[str, Any]]:
    static, found, dynamic = system.partition(CACHE_BREAK)
    if not found:
        return [{"type": "text", "text": system}]
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": static, "cache_control": {"type": "ephemeral"}}
    ]
    if dynamic:
        blocks.append({"type": "text", "text": dynamic})
    return blocks


class AnthropicClient:
    def __init__(self, *, api_key: str, model: str, base_url: str | None = None,
                 supports_images: bool = True, http_client: Any = None) -> None:
        self.supports_images = supports_images
        self._model = model
        # The SDK retries 408/409/429/5xx and connection errors with backoff.
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, base_url=base_url, max_retries=3, http_client=http_client
        )

    async def complete(self, system: str, messages: list[ChatMessage], tools: list[ToolDef],
                       max_tokens: int = 1024, temperature: float = 0.2) -> LLMResponse:
        # `temperature` is not sent: current Claude models reject sampling parameters.
        request: dict[str, Any] = {
            "model": self._model,
            "system": _system(system),
            "messages": _messages(messages),
            "max_tokens": max_tokens,
        }
        if tools:
            request["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
            ]
        try:
            response: Any = await self._client.messages.create(**request)
        except anthropic.APIError as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc

        text = "".join(b.text for b in response.content if b.type == "text")
        calls = [
            ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
            for b in response.content if b.type == "tool_use"
        ]
        usage = response.usage
        cached = getattr(usage, "cache_read_input_tokens", None) or 0
        written = getattr(usage, "cache_creation_input_tokens", None) or 0
        return LLMResponse(
            text=text or None,
            tool_calls=calls,
            stop="tool_calls" if calls else _STOP.get(response.stop_reason, "error"),
            usage=Usage(input_tokens=usage.input_tokens + cached + written,
                        output_tokens=usage.output_tokens, cached_tokens=cached),
            opaque=response.content,
        )
