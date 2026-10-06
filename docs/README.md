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
2. Tap the invite it shows (a Telegram deep link, QR code and code). The bot answers "Hi <name>,
   you're connected" and asks its first setup question: who lives here.
3. Answer in your own words, or say "skip". It asks about routines, shops, staples, photos of the
   fridge and when to stay quiet, one at a time, then gets out of the way.
4. Tell it another adult lives there and it sends you an invite to pass on. Send `dashboard` for a
   login link to the web dashboard.

Telegram must reach the api over HTTPS to deliver webhooks; see [channels.md](channels.md).
Without Docker: [operations.md](operations.md#running-without-docker).

## What is built

Milestones 1 to 3 of the six in [spec.md](spec.md#17-deployment-and-build-milestones):
Telegram, inventory and the shopping list, undo, `/setup`, magic-link login; the calendar tools,
reminders that respect quiet hours, the daily brief and weekly digest, a read-only ICS feed; then
conversational onboarding, invites, media storage on S3 or ImgBB, receipt and fridge photos, and
the Family and Settings pages. Each page below says what exists now and what is deferred.

| Doc | Contents |
| --- | --- |
| [architecture.md](architecture.md) | Processes, message flow, media, quiet hours, scheduled jobs, repo layout |
| [data-model.md](data-model.md) | Tables, stock transitions, calendar rows, invariants |
| [channels.md](channels.md) | Telegram setup and payload notes; the adapter contract |
| [llm.md](llm.md) | Provider layer, adding an adapter, tested models |
| [agent-and-tools.md](agent-and-tools.md) | Runtime loop, photos, onboarding, tools, calendar rules, resolution rules, undo |
| [dashboard.md](dashboard.md) | Pages, the login flow, invites, the calendar feed |
| [operations.md](operations.md) | Environment variables, running, media storage, worker jobs, logs |
| [evals.md](evals.md) | Running the agent evals, adding a case, latest results |
| [adr/](adr/) | One decision per file |
| [spec.md](spec.md) | The v1 implementation spec: the baseline for behaviour, names and layout |
