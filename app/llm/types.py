"""Provider-neutral LLM types (spec section 8.1). No vendor SDK type leaves app/llm/."""
from typing import Any, Literal, Protocol

from pydantic import BaseModel

# Marks where the static (cacheable) part of a system prompt ends. Adapters with prompt
# caching split here; every adapter removes it before sending.
CACHE_BREAK = "\n<cache-break/>\n"


class LLMError(Exception):
    """The provider call failed after the adapter's own retries."""


class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ImagePart(BaseModel):
    type: Literal["image"] = "image"
    mime: str
    data_b64: str


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]
    error: str | None = None               # set by the adapter when the arguments were unusable


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: list[TextPart | ImagePart] = []
    tool_calls: list[ToolCall] = []        # assistant only
    tool_call_id: str | None = None        # tool only
    is_error: bool = False                 # tool only
    opaque: Any = None                     # assistant only: LLMResponse.opaque, passed back untouched


class ToolDef(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]             # JSON Schema from the Pydantic args model


class Usage(BaseModel):
    input_tokens: int = 0                  # all prompt tokens, cached ones included
    output_tokens: int = 0
    cached_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_tokens=self.cached_tokens + other.cached_tokens,
        )


class LLMResponse(BaseModel):
    text: str | None
    tool_calls: list[ToolCall] = []
    stop: Literal["end", "tool_calls", "length", "error"]
    usage: Usage = Usage()
    # Adapter-private state the provider wants back with this assistant turn (for example
    # reasoning blocks). Callers copy it onto the assistant ChatMessage and never read it.
    opaque: Any = None


class LLMClient(Protocol):
    supports_images: bool

    async def complete(self, system: str, messages: list[ChatMessage], tools: list[ToolDef],
                       max_tokens: int = 1024, temperature: float = 0.2) -> LLMResponse: ...
