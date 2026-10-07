# 0032 Subscription providers run the vendors' own CLIs

## Context

The household already pays for a Claude subscription and a ChatGPT plan. Both vendors let their
coding CLI run on that sign-in, and several coding agents offer "use your subscription" as a model
provider. The ask was to have the same choice here, switched by `LLM_PROVIDER` like the others.

How those agents do it was read from their source before anything was built:

- **Claude.** Every harness still offering a Claude subscription wraps Anthropic's own binary:
  Cline through `ai-sdk-provider-claude-code`, which is built on Anthropic's Agent SDK, which
  spawns `claude`. The ones that sent a subscription's OAuth token to Anthropic's API from their
  own client have removed it; OpenCode's docs say "Anthropic explicitly prohibits this", and
  Anthropic's legal page does.
- **ChatGPT.** Cline's default, OpenCode, Roo Code and LiteLLM all call OpenAI's Codex backend
  directly, with the Codex CLI's OAuth client id and tokens they store and refresh themselves.
  Cline also ships a second provider that wraps `codex exec`.

Both vendors publish a Python SDK that launches their CLI (`claude-agent-sdk`, `openai-codex`),
and both were tried.

What each vendor's terms say about this use is unclear rather than forbidding. The reading, with
quotes, is in [llm.md](../llm.md#subscription-providers).

## Decision

**Two providers, `claude_code` and `codex_cli`, each one model step through the vendor's own
unmodified CLI, headless, on the sign-in that CLI already holds.** They are `LLMClient`s in
`app/llm/`; the loop, the tools and everything else are untouched.

- **The official CLI for both, not OAuth tokens.** For Claude there is no other permitted way.
  For ChatGPT, direct OAuth is what most harnesses do, and it would be faster and have native
  tool calls; it was not chosen because OpenAI's docs describe `codex exec` on a ChatGPT sign-in
  and do not describe third-party OAuth clients, because this project would then hold and
  refresh the tokens and follow an undocumented endpoint itself, and because a household
  assistant presenting itself as a Codex client is further from what OpenAI tolerates than a
  coding agent is. With the CLI, no token is read or sent by this project's code.
- **The documented headless commands, `claude -p` and `codex exec`, not the SDKs.** The SDKs
  start the same binaries. For one stateless request they add what then has to be switched off:
  the Codex SDK's app server loaded the personal `config.toml` and offered the model that
  user's MCP servers, and the Claude SDK always passes the parent's environment to the CLI. They
  were no faster, and each pins and bundles its own 230 to 310 MB copy of the binary.
- **The CLI runs no tool of its own; the loop still runs the household's.** One CLI process is
  one model step. The household's tools write inside the turn's transaction and leave undo
  records, so they stay in this process, where the loop's guards are.
- **Claude gets the tools as functions, through a stand-in MCP server.** `claude -p` runs with
  `--tools ""`, a server that lists the household's tools and runs none (`app/llm/tool_stub.py`),
  and `--max-turns 1`, so the run ends once the model has said which to call. The adapter reads
  the calls from the output stream. The first build asked for the step as JSON through
  `--json-schema` instead. That was worse: told about tools it could not call, the model tried
  to call them anyway, was refused by the CLI, and only then answered, so a step cost two or
  three requests and, with the tools described only in the schema, it sometimes gave up and
  made an answer up.
- **Codex answers with the step as JSON.** It has no way to be given functions it does not run
  itself. The tools and the answer's shape go in its instructions, and `--output-schema` holds it
  to `{"text", "tool_calls": [{"name", "arguments"}]}`. That schema allows no free-form object,
  so arguments are JSON text, parsed here; bad JSON becomes the same `invalid JSON arguments`
  tool error the API adapters give.
- **History is one prompt.** A CLI takes a prompt, not a list of turns, so the conversation is
  rendered as one JSON object per line. A person's message cannot pass for a tool result or an
  earlier step, because it cannot leave its line. The static prompt is the CLI's system prompt
  and the household brief leads the message, so the part that never changes is the part cached.
- **Images** go to Claude as image blocks in the message and to Codex as files in the scratch
  directory, passed with `--image`.
- **Nothing of the machine's own joins a turn.** Claude: `--restricted`, `--setting-sources ""`,
  `--strict-mcp-config`, `--disable-slash-commands`, `--no-session-persistence`, auto-memory and
  self-update off. Codex: `--ignore-user-config`, `--ignore-rules`, `--ephemeral`, a read-only
  sandbox, web search off, its shell, code-mode, image, browser, hook, memory, plugin and app
  features disabled, and a one-token budget for its skills list, which has no switch. Each run's working directory is a new empty temporary directory, removed
  afterwards. The CLI's environment is `PATH`, `HOME`, locale, proxy and CA variables and its own
  (`CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_OAUTH_TOKEN`, `CODEX_HOME`): not the app's secrets, and no
  API key, which would move the calls off the subscription.
- **The isolation is checked on every answer.** Claude's stream begins with the tools it was
  started with; any set other than exactly the household's discards the answer. Any item in
  Codex's event stream that is not a message, reasoning or a warning (a command, a file change,
  an MCP or web call) discards the answer.
- **Failures are `LLMError` with one line of what the CLI said**, and two are named: a missing or
  expired sign-in, and a usage limit. The System page shows the last failed turn's error, so an
  expired sign-in is visible there. The CLIs retry transient API errors themselves; the adapter
  does not retry.
- **A step is stopped after `LLM_CLI_TIMEOUT` seconds (120)**, its process group killed. At most
  two CLI processes run at once per process.
- **`max_tokens` and `temperature` are not passed.** Neither CLI has a flag for them.

## Consequences

- Switching is `LLM_PROVIDER` and `LLM_MODEL`. No key and no endpoint; the CLI must be installed
  and signed in where the api and the worker run.
- It is slower than an API: each step starts a program. On the eval suite a turn took 8.7
  seconds on average through `claude_code` and 11.9 through `codex_cli`, against about 3 through
  the API adapters ([evals.md](../evals.md#the-subscription-providers)).
- The Claude adapter depends on three things the CLI does today: an MCP server given on the
  command line works under `--restricted`, the `init` event lists the tools, and reaching
  `--max-turns` leaves the tool calls in the stream. The Codex adapter depends on feature names
  that an unknown-flag error would reject on a version that has dropped one. Both fail closed,
  with the CLI's message, and the recorded runs in `tests/contract/fixtures/llm_cli` are how a
  new CLI version is checked.
- Each CLI adds context of its own to every request: about 500 tokens for Claude Code and about
  2,000 for Codex, measured on a near-empty request. It counts against the plan.
- The CLI tells the model a few things on its own: the working directory, the model's name,
  today's date, and for Claude the email address of the signed-in account. The date is the
  machine's, so the prompt says to go by the brief's.
- Codex still lists a few tools to the model that it then refuses to run. A step in which the
  model tries one is discarded, not answered.
- The household's turns draw on the same allowance as the owner's own use of Claude or ChatGPT,
  and stop when it runs out.
- Direct OAuth to the Codex backend remains possible without code here: a LiteLLM proxy's
  `chatgpt/` models behind `LLM_PROVIDER=openai_compat`. It was not tried, and
  [ADR 0002](0002-own-llm-layer-instead-of-litellm.md) is why LiteLLM is not a dependency.
