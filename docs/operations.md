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
| `LLM_SUPPORTS_IMAGES` | no | `true` | `false`: photos are not sent to the model and the agent says it cannot read them |
| `LLM_MAX_TOOL_ITERATIONS` | no | `8` | Loop guard |
| `STT_PROVIDER`, `STT_BASE_URL`, `STT_API_KEY`, `STT_MODEL` | no | | Voice-note transcription (`openai_compat`) |
| `MEDIA_BACKEND` | no | `s3` | `s3` or `imgbb`. The backend is used only once its variables are set |
| `S3_ENDPOINT`, `S3_BUCKET`, `S3_ACCESS_KEY`, `S3_SECRET_KEY` | for `s3` | | Any S3-compatible bucket. Leave `S3_ENDPOINT` unset for AWS |
| `S3_REGION` | no | `us-east-1` | Signing region; MinIO accepts any |
| `IMGBB_API_KEY` | for `imgbb` | | Image hosting; images only |
| `MEDIA_RETENTION_DAYS` | no | `90` | Stored media is deleted after this; ImgBB caps it at 180 |
| `TG_BOT_TOKEN`, `TG_BOT_USERNAME`, `TG_WEBHOOK_SECRET` | for Telegram | | Bot API; the username builds invite links |
| `WA_PHONE_NUMBER_ID`, `WA_ACCESS_TOKEN`, `WA_APP_SECRET`, `WA_VERIFY_TOKEN` | for WhatsApp | | Cloud API. The app secret checks webhook signatures; the verify token answers Meta's subscription check |
| `WA_API_VERSION` | no | `v26.0` | Graph API version in every WhatsApp call. Meta retires a version about two years after release |
| `WA_REMINDER_TEMPLATE` | no | `household_reminder` | The approved utility template that carries a message outside the 24-hour window |
| `AGENT_NAME` | no | `Home` | Name used in the prompt and pages |
| `DEFAULT_TIMEZONE` | no | `Europe/London` | Prefilled on `/setup` |
| `DEBOUNCE_SECONDS` | no | `4` | How long a batch must be quiet before its turn |
| `LOG_LEVEL` | no | `INFO` | |

Variables for later milestones (iMessage, Letta) are in
[spec.md section 3](spec.md#3-configuration-and-dependencies) and are not read yet.

## Running with Docker

`docker compose up --build` runs Postgres 16, `alembic upgrade head`, the api on port 8000 and the
worker. The image is `python:3.12-slim` with `ffmpeg` and runs as `nobody`. Compose does not run an
object store: point `S3_*` in `.env` at a bucket you have, or leave them unset and photos are not
read.

## Media storage

Photos and voice notes are kept through `MediaStore`
([ADR 0018](adr/0018-media-retention-and-photos-without-a-backend.md)).

- **`s3`**: a private bucket and a key pair that can put, get and delete objects in it. Objects are
  named `{household}/{message_id}/{n}.{ext}`. Add a bucket lifecycle rule a little longer than
  `MEDIA_RETENTION_DAYS` as a backstop.
- **`imgbb`**: one API key and no infrastructure. Images only; voice notes are transcribed and not
  kept. Anyone holding an image's URL can view it until it expires, and it cannot be deleted
  early. Receipts can show an address or part of a card number, so prefer `s3` for real use.
- **Neither set**: everything else works; a photo gets a reply saying it could not be read.

Message text and photos also go to the configured LLM provider. Check its retention settings.

```sql
-- stored media by age
select date_trunc('month', created_at) as month, count(*) from messages
where jsonb_path_exists(media, '$[*].storage_key') group by 1 order by 1;
```

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

## Worker jobs

`python -m app.worker.main` runs `inbound`, `outbox`, `fire_reminders` (every 15 s),
`expand_recurrence` (hourly), `daily_brief` and `weekly_digest` (checked every minute) and, when a
media backend is configured, `media_cleanup` (hourly). What each
does and why running it twice is harmless is in
[architecture.md](architecture.md#scheduled-jobs). Log events worth watching:
`reminder_queued`, `digest_queued`, `outbox_sent`, `outbox_held_for_quiet_hours`,
`outbox_send_failed`, `outbox_delivery_failed`, `outbox_fallback_queued`, `group_created`,
`group_not_created`, `media_failed`, `photo_unreadable`, `media_cleaned`, `media_cleanup_failed`,
`invite_redeemed`, `job_crashed`.

```sql
-- reminders that should have gone and have not
select id, text, fire_at from reminders where status = 'scheduled' and fire_at < now() - interval '2 minutes';
-- what the digests have run
select job, run_key, ran_at from job_runs order by ran_at desc limit 20;
-- sends waiting for quiet hours to end
select id, target, send_after from outbox where status = 'pending' and send_after > now();
-- sends that failed for good, the channel tried, and whether a second channel took them
select o.created_at, o.channel_used, o.last_error, f.status as fallback, f.channel_used as fallback_channel
from outbox o left join outbox f on f.dedupe_key = 'fallback:' || o.id
where o.status = 'failed' order by o.created_at desc limit 20;
```

## WhatsApp

Setup and the template are in [channels.md](channels.md#whatsapp). The Channels page shows, per
chat, whether WhatsApp will take an ordinary message or only the template, and how many sends
failed in the last day.

### WhatsApp template rejected or paused

Outside 24 hours of someone's last message, every text is sent inside `WA_REMINDER_TEMPLATE`.
While that template is not approved, those sends fail with a Graph error such as
`132001 Template name does not exist in the translation` and move to the member's next channel;
a member with only WhatsApp gets nothing until they write again. Replies are not affected,
because a reply is always inside the window.

1. In WhatsApp Manager, Message templates, read the status and the rejection reason.
2. Rejected: the usual causes are a body that starts or ends with the variable, a category other
   than Utility, or wording that reads as marketing. Edit and resubmit, or create a new template
   and set `WA_REMINDER_TEMPLATE` to its name, then restart the worker.
3. Paused or disabled for low quality: Meta lifts a pause by itself after some hours; a disabled
   template has to be replaced.
4. Language must be English (UK): the adapter sends `en_GB`.
5. Failed sends are on the Activity page with "Retry". A retry within 24 hours of the person
   writing goes out as an ordinary message.

### An expired or revoked WhatsApp token

Every WhatsApp call fails with Graph error `190`. Sends fail at once and move to each member's
next channel, incoming photos and voice notes cannot be downloaded (the turn still runs and says
so), and group creation shows the error. Set a new `WA_ACCESS_TOKEN` and restart both processes.
Incoming text keeps working throughout, because webhooks are checked with the app secret.

The brief time is `households.digest_time` and quiet hours are `members.quiet_start` and
`quiet_end`, all in household time. Change them on the dashboard Settings page or by telling the
assistant ("make the morning brief 7", "don't message me after 9pm").

## Logs

JSON lines via structlog, carrying `household_id` and `message_id` where known and never message
text. `httpx` request logging is silenced because the Telegram URL contains the bot token. The
WhatsApp token travels in a header and is never in a URL or an error message.

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

Helm chart, backups, rotating presence tokens, and the outage runbook for BlueBubbles belong to
later milestones. The calendar feed token is rotated from the Calendar page, and an
invite is replaced or revoked on the Family page.
