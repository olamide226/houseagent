# LLM layer

The agent talks to `LLMClient` (`app/llm/types.py`), never to a vendor SDK. Each adapter translates
neutral types to one wire format. `LLM_PROVIDER` picks the adapter; switching model is a config
change gated by the eval suite.

| `LLM_PROVIDER` | Talks to | Paid by |
| --- | --- | --- |
| `openai_compat` | Any OpenAI-compatible endpoint | An API key |
| `anthropic` | The Anthropic Messages API, or a compatible endpoint | An API key |
| `claude_code` | The `claude` CLI on this machine | A Claude subscription ([below](#subscription-providers)) |
| `codex_cli` | The `codex` CLI on this machine | A ChatGPT plan ([below](#subscription-providers)) |

| Neutral concept | `openai_compat` | `anthropic` |
| --- | --- | --- |
| System prompt | First message, role `system` | Top-level `system`; the static part is marked cacheable |
| `ToolDef` | `tools[{type: "function", function}]` | `tools[{name, description, input_schema}]` |
| Tool call | `message.tool_calls[].function.arguments` (JSON string) | `tool_use` blocks |
| Tool result | Message with role `tool` | `tool_result` block in a user turn, with `is_error` |
| `ImagePart` | `image_url` with a `data:` URI | `image` block with a base64 source |
| Stop | `finish_reason` | `stop_reason` |

`make_llm(settings, fast=True)` gives a client for `LLM_FAST_MODEL` (default: `LLM_MODEL`) on the
same provider. The worker uses it for the one background chore that needs a model: sorting items
with no category into supermarket sections, with a JSON answer and no tools.

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
  turn, and adjacent same-role turns merge. That API wants a user turn first, so when a thread's
  history begins with something the assistant said (a reminder, the low-stock prompt), a one-line
  placeholder user turn goes in front of it; the message itself is kept, because the reply to it
  means nothing without it.
- **Images.** The loop loads a turn's photos through `MediaStore` and sends them as `ImagePart`s
  (base64, with the stored mime type) after the turn's text. `LLM_SUPPORTS_IMAGES=false` makes it
  send a note instead: `[photo received; this model can't read photos]`.
- **Temperature** is not sent on the Anthropic path: current Claude models reject sampling parameters.
- `stop_reason: refusal` and unknown stop reasons map to `stop = "error"`, which fails the turn.

`LLM_BASE_URL` is required for `openai_compat` and optional for `anthropic`, where it points the
adapter at an Anthropic-compatible endpoint.

## Subscription providers

`claude_code` and `codex_cli` run each model step through the vendor's own CLI, headless, on the
sign-in that CLI already has. There is no key and no endpoint: `LLM_MODEL` is a name the CLI
accepts (`haiku`, `sonnet`; `gpt-5.6-luna`), and `LLM_CLI_PATH` says where the CLI is if it is not
on `PATH`. The reasons for this shape, and what was tried first, are in
[ADR 0032](adr/0032-subscription-providers-through-the-vendors-clis.md). Installing the CLI and
getting its sign-in into a container is in [operations.md](operations.md#subscription-providers).

| Neutral concept | `claude_code` | `codex_cli` |
| --- | --- | --- |
| One step | `claude --print`, one turn (`--max-turns 1`) | `codex exec --json` |
| System prompt | The static part, as `--system-prompt-file` | The static part, then how to answer and the tools, as `model_instructions_file` |
| Household brief | Leads the message | Leads the prompt |
| History | One JSON object per line in the message | The same, in the prompt on stdin |
| `ToolDef` | A function of a stand-in MCP server that runs nothing | Listed in the instructions; `--output-schema` fixes the answer's shape |
| Tool call | `tool_use` blocks in the output stream | `tool_calls` in the JSON answer, arguments as JSON text |
| Tool result | A `tool` line in the next step's message | The same |
| `ImagePart` | An `image` block in the message | A file in the scratch directory, `--image` |
| Usage | The `result` event; cache reads and writes count as input | The `turn.completed` event |

- **The CLI runs none of its own tools.** Claude starts with `--tools ""` and nothing of the
  machine's settings; Codex starts read-only with its shell and its other features off and the
  user's `config.toml` ignored. Each run gets a new empty directory and an environment without
  the app's secrets. If Claude reports any tool but the household's, or Codex's events show it
  doing anything but answering, the answer is discarded and the turn fails.
- **No API key reaches the CLI**, so a stray `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` in the
  environment cannot move the calls onto pay-as-you-go.
- **Errors** are `LLMError` with one line of what the CLI said. `... is not signed in, or its
  sign-in has expired` and `... has reached the subscription's usage limit` are named as such;
  the System page shows the last failed turn's error.
- **Timeouts.** A step that takes longer than `LLM_CLI_TIMEOUT` (120 seconds) is stopped.
- **`max_tokens` and `temperature`** are ignored: neither CLI takes them.
- **Slower than an API.** Each step starts a program. See [evals.md](evals.md#latest-results)
  for the measured time per turn.

### What the vendors' terms say

Read on 7 October 2026. Neither vendor forbids this use in terms; neither clearly allows it for a
household. **Using a subscription this way is the account holder's risk to take**, and the API-key
providers remain the default.

Anthropic documents `claude -p` and a subscription token for scripts ("For CI pipelines, scripts,
or other environments where interactive browser login isn't available, generate a one-year OAuth
token with `claude setup-token`",
[Authentication](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token)). Its
[legal page](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use)
also says:

> "OAuth authentication is intended exclusively for purchasers of Claude Free, Pro, Max, Team,
> and Enterprise subscription plans and is designed to support ordinary use of Claude Code and
> other native Anthropic applications."
>
> "Developers building products or services that interact with Claude's capabilities, including
> those using the Agent SDK, should use API key authentication ... Anthropic does not permit
> third-party developers to offer Claude.ai login into their own applications, or to route
> requests through Free, Pro, or Max plan credentials on behalf of their users."
>
> "Nor does it prevent an end user from signing in to the unmodified Claude Code binary with their
> own Claude subscription"

OpenAI documents `codex exec` on a ChatGPT sign-in as an advanced path
([Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode),
[Maintain Codex account auth in CI/CD](https://learn.chatgpt.com/docs/auth/ci-cd-auth)):

> "API keys are the right default for automation because they are simpler to provision and rotate.
> Use this path only if you specifically need to run as your Codex account."
>
> "This is an advanced workflow for enterprise and other trusted private automation."

What is unclear, for both: the assistant also answers the account holder's partner and children,
and both vendors' consumer terms say not to "make your account available to anyone else". The
other members never hold the credentials or reach the vendor themselves, but their messages are
answered on the owner's plan. And a plan is priced for a person; Anthropic says its limits "assume
ordinary, individual usage".

What this project does to stay on the permitted side of what is clear: it runs the unmodified
official binary in its documented non-interactive mode, reads and sends no OAuth token itself,
calls no private endpoint, and has each install sign in with the installer's own account through
the vendor's own flow.

### Limits

A subscription has a usage allowance, not a bill, and the household shares it with the owner's own
use. Claude: "all activity in both tools counts against the same usage limits", in a five-hour
and a weekly window. Codex: "Local messages and cloud chats share your plan's usage allowance.
Weekly limits may also apply"; Plus also has a five-hour window. At the limit, turns fail with
the usage-limit error until the window resets. Anthropic announced, then paused in June 2026, a
change that would bill `claude -p` against a separate monthly credit instead
([support article](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan));
if it returns, this provider's cost changes.

## Adding an adapter

1. Add `app/llm/<name>.py` with a class that has `supports_images` and `complete(...)`.
2. Add its branch to `make_llm` in `app/llm/base.py` and its value to `Settings.llm_provider`.
3. Cover request and response translation in `tests/unit/test_llm_adapters.py`.
4. Run the evals against it ([evals.md](evals.md)).

The SDKs (`openai`, `anthropic`) are optional extras, `llm-openai` and `llm-anthropic`. Both are
built on `httpx2`, so tests fake the provider with an `httpx2.MockTransport`, not `respx`.

The subscription adapters have no SDK. Their tests (`tests/unit/test_llm_cli.py`) put a stand-in
`claude` and `codex` on `PATH` that records how it was run and prints what the test planned, and
`tests/contract/test_llm_cli.py` replays recorded runs of the real CLIs. After a CLI upgrade,
record again and run the evals.

## Speech-to-text

`SpeechToText` (`app/llm/stt.py`) has one method, `transcribe(audio, mime) -> str`. The shipped
adapter posts to any OpenAI-compatible `/audio/transcriptions` endpoint. The `local` provider from
the spec is not built. Tests use a stand-in transcriber.

## Tested models

See [evals.md](evals.md#latest-results) for the latest eval scores per provider.
