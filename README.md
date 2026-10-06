# Household Agent

A household assistant two adults talk to over chat. You say "we're out of eggs and bread" in a DM
or the family group; it records the stock change, puts staples on the shopping list, and reacts
instead of replying. "Ada has GP on Wednesday at 10:30" goes on the calendar, and
she is reminded the day before and an hour before. "Undo" reverts the last thing it did. Nobody
fills in a form.

One FastAPI service, one worker process, one Postgres database. A new channel is one adapter
module; a new model is a configuration change.

## What it does

- **Stock and shopping.** Text, voice notes, receipt photos and fridge photos become inventory
  events. Finishing a staple puts it on the shared list; buying it takes it off. Rules like
  these are code, not prompt.
- **Appointments and reminders.** One-off and recurring events, reminders that wait out quiet
  hours unless urgent, a morning brief, a Sunday digest, and a read-only calendar feed for Apple
  or Google Calendar.
- **At the shop.** An iPhone Shortcut tells the service you walked into a shop and the list for
  that shop arrives. It learns how long things last and asks about what is probably running low.
- **Channels.** Telegram, WhatsApp (Cloud API) and iMessage (through a BlueBubbles server on a
  Mac you own). A person's messages move between channels by changing one row, and move by
  themselves when a channel fails.
- **A small dashboard.** Setup, the list, stock, the calendar, the family and their invites,
  channels, an activity log with undo, a playground, settings, and a System page for the admin.
  Login is a one-time link the assistant sends you in chat.
- **Nothing leaves the household.** No tool can address a recipient: the router only sends to
  chats and handles the household has verified.

## Status

All six milestones of the [spec](docs/spec.md) are implemented. **It has not been used by a
household yet**, and how much of it has met the real services differs by part:

| Part | State |
| --- | --- |
| Inventory, shopping list, undo, calendar, reminders, digests, onboarding, dashboard | Built and tested against Postgres, with live-model evals |
| Telegram | Built; tested against recorded payloads and mocked sends, never against Telegram itself |
| WhatsApp | Built; tested against payloads built from Meta's docs and mocked calls. There is no WhatsApp account yet |
| iMessage | Built from the BlueBubbles server source; tested against payloads built from it. There is no Mac set up yet |
| Presence | Built; tested with plain HTTP calls, never from a phone |
| Voice notes | Built; tested with a stand-in transcriber. No speech-to-text service has been tried |
| Agent quality | Measured on 50 eval cases with one model through two adapters. The release bar is not met: see [docs/evals.md](docs/evals.md#latest-results) |
| Letta runtime | Optional and off by default. Built, compared with the plain loop on the eval suite, and not promoted: see [docs/evals.md](docs/evals.md#the-letta-comparison) |
| Helm chart | Linted and rendered; never installed on a cluster |

Each page under `docs/` says, for its feature, what has been checked and what has not.

## Run it locally

Needs Docker, a Telegram bot token from BotFather, and a key for any tool-calling LLM.

```sh
cp .env.example .env          # fill in LLM_*, TG_*, SESSION_SECRET, SETUP_TOKEN
docker compose up --build     # Postgres, migration, api on :8000, worker
```

Open `http://localhost:8000/setup?token=<SETUP_TOKEN>`, create the household, and tap the invite
it shows. The assistant says hello and asks its first setup question. Telegram must reach the api
over HTTPS to deliver webhooks. The longer version, and running without Docker, is in
[docs/README.md](docs/README.md).

```sh
createdb houseagent_test      # once
uv sync --all-extras
uv run ruff check app tests && uv run mypy app/core app/llm app/agent
uv run pytest tests/unit tests/contract
```

## Where things are

| | |
| --- | --- |
| [docs/README.md](docs/README.md) | The five-minute start and an index of the docs |
| [docs/architecture.md](docs/architecture.md) | The two processes, how a message flows, the router, the scheduled jobs |
| [docs/channels.md](docs/channels.md) | Setting up Telegram, WhatsApp and iMessage; payload notes; limits |
| [docs/agent-and-tools.md](docs/agent-and-tools.md) | The runtime, the prompt, the twelve tools, undo, the Letta runtime |
| [docs/dashboard.md](docs/dashboard.md) | Every page, login, invites |
| [docs/presence.md](docs/presence.md) | The iPhone Shortcut, the arrival rules, predictions |
| [docs/operations.md](docs/operations.md) | Environment variables, Helm, health, the BlueBubbles outage and WhatsApp template runbooks, logs, costs |
| [docs/evals.md](docs/evals.md) | The eval suite, how to run it, the latest results |
| [docs/data-model.md](docs/data-model.md), [docs/llm.md](docs/llm.md) | Tables and invariants; the model layer |
| [docs/adr/](docs/adr/) | Thirty decisions, one per file |
| [docs/spec.md](docs/spec.md) | The v1 implementation spec this was built from |

```text
app/
  channels/    Telegram, WhatsApp and iMessage adapters behind one contract
  pipeline/    inbound (persist, debounce, turn), media, the outbound router
  agent/       the loop runtime, the optional Letta runtime, prompt, tools, undo
  services/    the one write path, shared by tools and the dashboard
  dashboard/   pages and login        presence/  the Shortcut endpoint
  worker/      the job supervisor     llm/       two provider adapters, speech-to-text
deploy/helm/household-agent/          tests/     unit, contract, evals
```
