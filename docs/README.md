# Household Agent docs

The agent is called Home (configurable). You tell it "we're out of eggs and bread" in a DM or the
family group; it records the stock change, puts staples on the shopping list, and reacts with a
thumbs-up instead of replying. "Ada has GP on Wednesday at 10:30" goes on the calendar, and she
is reminded the day before and an hour before. "Undo" reverts the last thing it did.

## Five-minute local start

Needs Docker, a Telegram bot token from BotFather, and a key for any tool-calling LLM.

```sh
cp .env.example .env          # fill in LLM_*, TG_*, SESSION_SECRET, SETUP_TOKEN
docker compose up --build     # Postgres, migration, api on :8000, worker
```

1. Open `http://localhost:8000/setup?token=<SETUP_TOKEN>` and create the household.
2. Tap the invite it shows (a Telegram deep link, QR code and code). The bot answers "Hi <name>, you're connected."
3. Tell the bot what you have or what ran out. Send `dashboard` for a login link to the web dashboard.

Telegram must reach the api over HTTPS to deliver webhooks; see [channels.md](channels.md).
Without Docker: [operations.md](operations.md#running-without-docker).

## What is built

Milestones 1 and 2 of the six in [spec.md](spec.md#17-deployment-and-build-milestones):
Telegram, inventory and the shopping list, undo, `/setup`, magic-link login; then the calendar
tools, reminders that respect quiet hours, the daily brief and weekly digest, a read-only ICS
feed, and the Calendar dashboard page. Each page below says what exists now and what is deferred.

| Doc | Contents |
| --- | --- |
| [architecture.md](architecture.md) | Processes, message flow, quiet hours, scheduled jobs, repo layout |
| [data-model.md](data-model.md) | Tables, stock transitions, calendar rows, invariants |
| [channels.md](channels.md) | Telegram setup and payload notes; the adapter contract |
| [llm.md](llm.md) | Provider layer, adding an adapter, tested models |
| [agent-and-tools.md](agent-and-tools.md) | Runtime loop, tools, calendar rules, resolution rules, undo |
| [dashboard.md](dashboard.md) | Pages, the login flow, the calendar feed |
| [operations.md](operations.md) | Environment variables, running, worker jobs, logs |
| [evals.md](evals.md) | Running the agent evals, adding a case, latest results |
| [adr/](adr/) | One decision per file |
| [spec.md](spec.md) | The v1 implementation spec: the baseline for behaviour, names and layout |
