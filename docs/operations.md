# Operations

## Environment variables

Everything is read by `Settings` in `app/config.py`, from the environment or a local `.env`.
`.env.example` lists them with placeholders. A channel is enabled only when its variables are set.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `DATABASE_URL` | yes | | `postgresql+asyncpg://...`, Postgres 15+ |
| `PUBLIC_BASE_URL` | yes | | Webhook and login links; `https://` also makes the session cookie Secure |
| `SESSION_SECRET` | yes | | Signs dashboard cookies and CSRF tokens |
| `SETUP_TOKEN` | first run | | Unlocks `/setup` until a household exists |
| `LLM_PROVIDER` | yes | | `openai_compat` or `anthropic` |
| `LLM_BASE_URL` | for `openai_compat` | | Endpoint; optional override for `anthropic` |
| `LLM_API_KEY` | yes (except local models) | | Provider key |
| `LLM_MODEL` | yes | | Must support tool calling |
| `LLM_SUPPORTS_IMAGES` | no | `true` | Recorded on the client; photos are not read yet |
| `LLM_MAX_TOOL_ITERATIONS` | no | `8` | Loop guard |
| `STT_PROVIDER`, `STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL` | no | | Voice-note transcription (`openai_compat`) |
| `TG_BOT_TOKEN`, `TG_BOT_USERNAME`, `TG_WEBHOOK_SECRET` | for Telegram | | Bot API; the username builds invite links |
| `AGENT_NAME` | no | `Home` | Name used in the prompt and pages |
| `DEFAULT_TIMEZONE` | no | `Europe/London` | Prefilled on `/setup` |
| `DEBOUNCE_SECONDS` | no | `4` | How long a batch must be quiet before its turn |
| `LOG_LEVEL` | no | `INFO` | |

Variables for later milestones (media storage, WhatsApp, iMessage, Letta) are in
[spec.md section 3](spec.md#3-configuration-and-dependencies) and are not read yet.

## Running with Docker

`docker compose up --build` runs Postgres 16, `alembic upgrade head`, the api on port 8000 and the
worker. The image is `python:3.12-slim` with `ffmpeg` and runs as `nobody`.

## Running without Docker

```sh
uv sync --all-extras
uv run alembic upgrade head
uv run uvicorn app.main:app --port 8000      # api
uv run python -m app.worker.main             # worker, in a second terminal
```

## Health

- `GET /healthz`: liveness.
- `GET /readyz`: 200 when the database is reachable and at migration `0001`, else 503.
- The worker logs `worker_heartbeat` every minute and restarts a crashed job after 5 seconds.

## Logs

JSON lines via structlog, carrying `household_id` and `message_id` where known and never message
text. `httpx` request logging is silenced because the Telegram URL contains the bot token.

## Cost tracking

Each turn's token usage is in `messages.meta.usage` and on the Activity page:

```sql
select date_trunc('day', created_at) as day,
       sum((meta->'usage'->>'input_tokens')::int)  as input_tokens,
       sum((meta->'usage'->>'output_tokens')::int) as output_tokens
from messages where meta ? 'usage' group by 1 order by 1 desc;
```

## Tests

```sh
createdb houseagent_test                     # once
uv run ruff check app tests
uv run mypy app/core app/llm app/agent
uv run pytest tests/unit tests/contract
```

Tests use local Postgres and refuse to run against any database not named `houseagent_test`
([ADR 0008](adr/0008-local-postgres-for-tests.md)). Set `TEST_DATABASE_URL` to change host or
user. The schema is dropped and rebuilt from migration `0001` at the start of each run.

## Secrets

Keep keys in an untracked `.env`. `.gitignore` excludes `.env` and `.env.*` except `.env.example`.
Nothing in the test suite, fixtures or eval results contains a credential.

## Not covered yet

Helm chart, backups, token rotation, and outage runbooks for WhatsApp and BlueBubbles belong to
later milestones.
