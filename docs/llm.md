# LLM layer

The agent talks to `LLMClient` (`app/llm/types.py`), never to a vendor SDK. Each adapter translates
neutral types to one wire format. `LLM_PROVIDER` picks the adapter; switching model is a config
change gated by the eval suite.

| Neutral concept | `openai_compat` | `anthropic` |
| --- | --- | --- |
| System prompt | First message, role `system` | Top-level `system`; the static part is marked cacheable |
| `ToolDef` | `tools[{type: "function", function}]` | `tools[{name, description, input_schema}]` |
| Tool call | `message.tool_calls[].function.arguments` (JSON string) | `tool_use` blocks |
| Tool result | Message with role `tool` | `tool_result` block in a user turn, with `is_error` |
| `ImagePart` | `image_url` with a `data:` URI | `image` block with a base64 source |
| Stop | `finish_reason` | `stop_reason` |

## Adapter rules

- **Malformed tool arguments** become a `ToolCall` with `error = "invalid JSON arguments"`; the
  loop returns that to the model as a tool error so it can retry.
- **Retries** on 408, 429, 5xx and connection errors are the vendor SDK's (`max_retries=3`).
  Anything that still fails is raised as the neutral `LLMError`.
- **Prompt caching.** The runtime puts `CACHE_BREAK` between the static prompt and the household
  brief. The Anthropic adapter splits there and marks the static block `cache_control: ephemeral`;
  the OpenAI adapter replaces the marker with a blank line.
- **Reasoning blocks.** Providers that return reasoning with a tool call want it back unchanged on
  the next request. `LLMResponse.opaque` carries the provider's own blocks; the loop copies it onto
  the assistant `ChatMessage` and never reads it. Only the adapter that produced it does.
- **Anthropic wire shape.** Tool results for one assistant turn are merged into a single user
  turn, adjacent same-role turns merge, and a leading assistant turn (from thread history) is dropped.
- **Images.** The loop loads a turn's photos through `MediaStore` and sends them as `ImagePart`s
  (base64, with the stored mime type) after the turn's text. `LLM_SUPPORTS_IMAGES=false` makes it
  send a note instead: `[photo received; this model can't read photos]`.
- **Temperature** is not sent on the Anthropic path: current Claude models reject sampling parameters.
- `stop_reason: refusal` and unknown stop reasons map to `stop = "error"`, which fails the turn.

`LLM_BASE_URL` is required for `openai_compat` and optional for `anthropic`, where it points the
adapter at an Anthropic-compatible endpoint.

## Adding an adapter

1. Add `app/llm/<name>.py` with a class that has `supports_images` and `complete(...)`.
2. Add its branch to `make_llm` in `app/llm/base.py` and its value to `Settings.llm_provider`.
3. Cover request and response translation in `tests/unit/test_llm_adapters.py`.
4. Run the evals against it ([evals.md](evals.md)).

The SDKs (`openai`, `anthropic`) are optional extras, `llm-openai` and `llm-anthropic`. Both are
built on `httpx2`, so tests fake the provider with an `httpx2.MockTransport`, not `respx`.

## Speech-to-text

`SpeechToText` (`app/llm/stt.py`) has one method, `transcribe(audio, mime) -> str`. The shipped
adapter posts to any OpenAI-compatible `/audio/transcriptions` endpoint. The `local` provider from
the spec is not built. Tests use a stand-in transcriber.

## Tested models

See [evals.md](evals.md#latest-results) for the latest eval scores per provider.
