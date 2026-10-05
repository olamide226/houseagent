# 0002 Own model-agnostic LLM layer instead of LiteLLM

## Context

The agent needs tool calling from more than one provider, and a model swap should be a config
change. LiteLLM covers many providers through one call but is a large, fast-moving dependency.

## Decision

`app/llm/` defines neutral types (`ChatMessage`, `ToolDef`, `ToolCall`, `LLMResponse`) and two
small adapters: OpenAI-compatible Chat Completions and Anthropic Messages. No vendor SDK type
leaves `app/llm/`. The vendor SDKs are optional extras, imported only by their adapter.

## Consequences

- Two adapters cover most providers, because most expose an OpenAI-compatible endpoint.
- Provider quirks (reasoning blocks to echo back, cache markers, merged tool results) are handled
  in one file each. See [0011](0011-opaque-provider-state-in-neutral-types.md).
- A `litellm` adapter can still be added behind the same protocol.
- The eval suite runs on two adapters, so accidental lock-in shows up as a failing test.
