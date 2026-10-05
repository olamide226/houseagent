"""OpenAI-compatible Chat Completions adapter (OpenAI, OpenRouter, Groq, Ollama, vLLM, ...)."""
import json
from typing import Any

import openai

from app.llm.types import (
    CACHE_BREAK,
    ChatMessage,
    ImagePart,
    LLMError,
    LLMResponse,
    TextPart,
    ToolCall,
    ToolDef,
    Usage,
)

_STOP = {"stop": "end", "tool_calls": "tool_calls", "length": "length"}


def _content(parts: list[TextPart | ImagePart]) -> Any:
    if all(isinstance(p, TextPart) for p in parts):
        return "\n".join(p.text for p in parts if isinstance(p, TextPart))
    return [
        {"type": "text", "text": p.text} if isinstance(p, TextPart)
        else {"type": "image_url", "image_url": {"url": f"data:{p.mime};base64,{p.data_b64}"}}
        for p in parts
    ]


def _message(m: ChatMessage) -> dict[str, Any]:
    if m.role == "tool":
        return {"role": "tool", "tool_call_id": m.tool_call_id, "content": _content(m.content)}
    wire: dict[str, Any] = {"role": m.role, "content": _content(m.content)}
    if m.tool_calls:
        wire["tool_calls"] = [
            {"id": c.id, "type": "function",
             "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
            for c in m.tool_calls
        ]
    return wire


def _tool_call(call: Any) -> ToolCall:
    try:
        arguments = json.loads(call.function.arguments or "{}")
        if not isinstance(arguments, dict):
            raise ValueError
    except ValueError:
        return ToolCall(id=call.id, name=call.function.name, arguments={}, error="invalid JSON arguments")
    return ToolCall(id=call.id, name=call.function.name, arguments=arguments)


class OpenAICompatClient:
    def __init__(self, *, api_key: str, model: str, base_url: str | None = None,
                 supports_images: bool = True, http_client: Any = None) -> None:
        self.supports_images = supports_images
        self._model = model
        # The SDK retries 408/429/5xx and connection errors with backoff.
        self._client = openai.AsyncOpenAI(
            api_key=api_key or "none", base_url=base_url, max_retries=3, http_client=http_client
        )

    async def complete(self, system: str, messages: list[ChatMessage], tools: list[ToolDef],
                       max_tokens: int = 1024, temperature: float = 0.2) -> LLMResponse:
        request: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "system", "content": system.replace(CACHE_BREAK, "\n\n")},
                         *(_message(m) for m in messages)],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            request["tools"] = [{"type": "function", "function": t.model_dump()} for t in tools]
        try:
            response: Any = await self._client.chat.completions.create(**request)
        except openai.APIError as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc

        choice = response.choices[0]
        calls = [_tool_call(c) for c in choice.message.tool_calls or []]
        usage = response.usage
        details = getattr(usage, "prompt_tokens_details", None)
        return LLMResponse(
            text=choice.message.content or None,
            tool_calls=calls,
            stop="tool_calls" if calls else _STOP.get(choice.finish_reason, "error"),
            usage=Usage(
                input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens,
                cached_tokens=getattr(details, "cached_tokens", None) or 0,
            ) if usage else Usage(),
        )
