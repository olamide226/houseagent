"""Provider-neutral LLM types (spec section 8.1). No vendor SDK type leaves app/llm/."""
import re
from typing import Any, Literal, Protocol

from pydantic import BaseModel

# Marks where the static (cacheable) part of a system prompt ends. Adapters with prompt
# caching split here; every adapter removes it before sending.
CACHE_BREAK = "\n<cache-break/>\n"


# Why a call failed, as the adapter could tell from what the provider said. "config" is one the
# admin has to fix (the key, the account, the model's name, the CLI's sign-in): trying again
# changes nothing. "transient" is one that passes by itself (a timeout, an overloaded service, a
# rate limit). An error with no kind was not recognised, and is treated as transient.
FailureKind = Literal["config", "transient"]
# The cause, which app/services/model_health.py says in plain words.
Cause = Literal["key_rejected", "key_missing", "not_allowed", "out_of_credit", "model_unknown", "signed_out",
                "cli_missing", "unreachable", "busy", "usage_limit"]
CAUSE_KIND: dict[str, FailureKind] = {
    "key_rejected": "config", "key_missing": "config", "not_allowed": "config", "out_of_credit": "config",
    "model_unknown": "config", "signed_out": "config", "cli_missing": "config",
    "unreachable": "transient", "busy": "transient", "usage_limit": "transient",
}


class LLMError(Exception):
    """The provider call failed after the adapter's own retries.

    `cause` is set when the adapter recognised the failure; `error_type` is the provider's own
    name for it (an error type or code, never its message), for the System page."""

    def __init__(self, message: str, *, cause: Cause | None = None, error_type: str | None = None) -> None:
        super().__init__(message)
        self.cause = cause
        self.error_type = error_type

    @property
    def kind(self) -> FailureKind | None:
        return CAUSE_KIND.get(self.cause) if self.cause else None


# What a provider says about a refused key can hold some of the key: OpenAI's "Incorrect API key
# provided: sk-abc1****wxyz", DeepSeek's "Your api key: ****wxyz is invalid". Anything shaped like
# a key, a masked key or a bearer token is taken out before an error is stored, shown or logged.
_SECRET = re.compile(
    r"\b(?:sk|rk|pk|sess|gsk|xai|key)[-_][\w*.-]{6,}"   # sk-..., sk-ant-..., key_...
    r"|[\w.-]*\*{3,}[\w*.-]*"                            # anything masked with asterisks
    r"|\bBearer\s+[\w.~+/-]{16,}=*"
    r"|\b(?=[A-Za-z_-]*\d)(?=[\d_-]*[A-Za-z])[A-Za-z0-9_-]{40,}\b", re.I)   # any other long token


def redact(text: str, *secrets: str | None) -> str:
    """`text` with the given secrets, and anything that looks like a key, replaced."""
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, "[redacted]")
    return _SECRET.sub("[redacted]", text)


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
