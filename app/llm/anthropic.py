"""Anthropic Messages adapter (also serves Anthropic-compatible endpoints via base_url)."""
from typing import Any

import anthropic

from app.llm.types import (
    CACHE_BREAK,
    Cause,
    ChatMessage,
    LLMError,
    LLMResponse,
    TextPart,
    ToolCall,
    ToolDef,
    Usage,
    redact,
)

_STOP = {"end_turn": "end", "stop_sequence": "end", "tool_use": "tool_calls", "max_tokens": "length"}
# The API wants a user turn first. A thread can begin with something we said (a reminder, a
# prompt), and the answer to it makes no sense without it, so it is kept behind this placeholder.
EARLIER = "[earlier messages not shown]"


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
        if not blocks:
            continue
        if not wire and role == "assistant":
            wire.append({"role": "user", "content": [{"type": "text", "text": EARLIER}]})
        if wire and wire[-1]["role"] == role:
            wire[-1]["content"] += blocks
        else:
            wire.append({"role": role, "content": blocks})
    return wire


# The error types of the Messages API (its errors page), by what the admin can do about them.
_CAUSES: dict[str, Cause] = {
    "authentication_error": "key_rejected", "permission_error": "not_allowed", "billing_error": "out_of_credit",
    "not_found_error": "model_unknown", "rate_limit_error": "busy", "overloaded_error": "busy",
    "api_error": "unreachable", "timeout_error": "unreachable",
}
_STATUS_CAUSES: dict[int, Cause] = {401: "key_rejected", 402: "out_of_credit", 403: "not_allowed",
                                    404: "model_unknown", 429: "busy", 529: "busy"}


def classify(exc: anthropic.APIError) -> tuple[Cause | None, str]:
    """What an SDK error means for the admin, and the API's own name for it."""
    if isinstance(exc, anthropic.APIConnectionError):   # a timeout is one too
        return "unreachable", type(exc).__name__
    if not isinstance(exc, anthropic.APIStatusError):
        return None, type(exc).__name__
    error_type = getattr(exc, "type", None) or type(exc).__name__
    if exc.status_code == 400 and "credit balance" in str(exc).lower():
        # An empty account has been refused as a plain bad request: "Your credit balance is too low".
        return "out_of_credit", error_type
    cause = _CAUSES.get(error_type) or _STATUS_CAUSES.get(exc.status_code)
    if cause is None and exc.status_code >= 500:
        cause = "unreachable"
    return cause, error_type


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
        self._key = api_key
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
        if not self._key:
            # The SDK would refuse with a TypeError before sending anything.
            raise LLMError("no API key is set (LLM_API_KEY)", cause="key_missing", error_type="no key")
        try:
            response: Any = await self._client.messages.create(**request)
        except anthropic.APIError as exc:
            cause, error_type = classify(exc)
            raise LLMError(redact(f"{type(exc).__name__}: {exc}", self._key), cause=cause,
                           error_type=error_type) from exc

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
