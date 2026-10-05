# 0011 Opaque provider state and a cache marker in the neutral LLM types

## Context

The spec's neutral types carry text, images and tool calls. Two provider behaviours do not fit:

- Reasoning models return reasoning blocks with a tool call and expect them back, unchanged, on the
  next request of the same turn. Dropping them fails or degrades the call.
- Anthropic prompt caching needs to know where the static part of the system prompt ends, but
  `complete()` takes the system prompt as one string.

## Decision

- `LLMResponse.opaque` and `ChatMessage.opaque` carry adapter-private state. The loop copies it
  from the response to the assistant message and never inspects it. Only the adapter that produced
  it reads it.
- `CACHE_BREAK` is a marker string the runtime places between the static prompt and the household
  brief. The Anthropic adapter splits on it and marks the first block cacheable; other adapters
  replace it with a blank line.
- `ToolCall.error` lets an adapter report unusable arguments so the loop can answer with a tool error.

## Consequences

- The `LLMClient.complete` signature from the spec is unchanged.
- No vendor type is named outside `app/llm/`; the opaque value is typed `Any`.
- Opaque state lives only for the turn. Thread history is rebuilt from text, so nothing
  provider-specific is persisted.
- Caching stays an optimisation: an adapter that ignores the marker still works.
