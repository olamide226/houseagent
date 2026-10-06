# Operations

## Environment variables

Everything is read by `Settings` in `app/config.py`, from the environment or a local `.env`.
`.env.example` lists them with placeholders. A channel is enabled only when its variables are set.

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `DATABASE_URL` | yes | | `postgresql+asyncpg://...`, Postgres 15+ |
| `PUBLIC_BASE_URL` | yes | | Webhook, presence and login links; `https://` also makes the session cookie Secure |
| `SESSION_SECRET` | yes | | Signs dashboard cookies and CSRF tokens |
| `SETUP_TOKEN` | first run | | Unlocks `/setup` until a household exists |
| `LLM_PROVIDER` | yes | | `openai_compat` or `anthropic` |
| `LLM_BASE_URL` | for `openai_compat` | | Endpoint; optional override for `anthropic` |
| `LLM_API_KEY` | yes (except local models) | | Provider key |
| `LLM_MODEL` | yes | | Must support tool calling |
| `LLM_FAST_MODEL` | no | `LLM_MODEL` | A cheaper model for the nightly job that sorts items into categories |
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
| `BB_BASE_URL`, `BB_PASSWORD`, `BB_WEBHOOK_SECRET` | for iMessage | | The BlueBubbles server on the Mac, its password, and the secret in the webhook URL it calls |
| `BB_PRIVATE_API` | no | `false` | `true` when the BlueBubbles Private API helper is installed: tapbacks and threaded replies |
| `AGENT_RUNTIME` | no | `loop` | `loop`, or `letta` for the optional Letta runtime ([agent-and-tools.md](agent-and-tools.md#the-letta-runtime-optional)) |
| `LETTA_BASE_URL`, `LETTA_API_KEY`, `LETTA_MODEL` | for `letta` | | The Letta server, its key if it wants one, and the model's handle as that server names it |
| `INTERNAL_TOOL_TOKEN` | for `letta` | | The bearer token Letta's tools present to `/internal/tools/*`. Unset, that route answers 404 |
| `INTERNAL_BASE_URL` | no | `PUBLIC_BASE_URL` | Where Letta reaches the api's `/internal` (Playground turns) |
| `WORKER_INTERNAL_URL` | no | `http://localhost:8001` | Where Letta reaches the worker's `/internal` (chat turns); the worker listens on this port under `letta` |
| `AGENT_NAME` | no | `Home` | Name used in the prompt and pages |
| `DEFAULT_TIMEZONE` | no | `Europe/London` | Prefilled on `/setup` |
| `DEBOUNCE_SECONDS` | no | `4` | How long a batch must be quiet before its turn |
| `LOG_LEVEL` | no | `INFO` | |
| `WORKER_HEARTBEAT_FILE` | no | `/tmp/worker-heartbeat` | The file the worker touches every minute, for its liveness probe |
| `EVAL_RESULTS_DIR` | no | `tests/evals/.results` | Where the System page looks for the last eval result |

Of the spec's variables, `DASHBOARD_PUBLIC` is not read: it is the Helm value
`dashboard.public` instead. `STT_PROVIDER` only accepts `openai_compat`; `local` is not built.

## Running with Docker

`docker compose up --build` runs Postgres 16, `alembic upgrade head`, the api on port 8000 and the
worker. The image is `python:3.12-slim` with `ffmpeg` and runs as `nobody`. Compose does not run an
object store: point `S3_*` in `.env` at a bucket you have, or leave them unset and photos are not
read.

## Deploying with Helm

The chart is `deploy/helm/household-agent/`: one image, an `api` Deployment with its Service and
an ingress, and a `worker` Deployment. Postgres is not in the chart: run it with CloudNativePG or
use a managed one, and put its URL in the Secret.

```sh
helm lint deploy/helm/household-agent
helm template home deploy/helm/household-agent -f my-values.yaml      # read what would be applied
helm upgrade --install home deploy/helm/household-agent -f my-values.yaml
```

**It has been linted and rendered, not installed.** No cluster was available when it was written
([ADR 0030](adr/0030-helm-chart-shape.md)), and the image is not published anywhere yet: build it
from the `Dockerfile`, push it to your registry, and set `image.repository` and `image.tag`.

- **Secrets are referenced, never embedded.** `existingSecret` names a Secret you create (with
  Sealed Secrets, for example) whose keys are environment variable names: `DATABASE_URL`,
  `SESSION_SECRET`, `SETUP_TOKEN`, `LLM_API_KEY` and each channel's tokens. The chart creates no
  Secret and renders no credential. Everything else goes in `config`, which becomes a ConfigMap;
  both are loaded into both processes.
- **api:** one replica, `maxSurge: 1` and `maxUnavailable: 0`, so a new pod is ready before the
  old one stops and a deploy drops no webhook. Liveness is `/healthz`, readiness `/readyz`
  (database reachable and migrated). An init container runs `alembic upgrade head` first; set
  `api.migrate: false` to run migrations yourself. `api.autoscaling.enabled` adds an HPA (CPU
  70%, 1 to 3 replicas).
- **worker:** one replica. Its liveness probe checks that the heartbeat file
  (`worker.heartbeatFile`) was touched in the last `worker.heartbeatMaxAgeSeconds` (180); the
  worker touches it every minute. A second replica is safe, not useful for one household.
- **Ingress:** only these paths reach the api: `/webhooks`, `/presence`, `/ics`, `/healthz`, and
  for the dashboard `/setup`, `/login`, `/logout`, `/dashboard`, `/static`. `/internal` and
  `/readyz` are never routed. With `dashboard.public: false` the dashboard paths are left off
  that ingress, and `dashboard.internalIngress` can serve them on another ingress class, such as
  a tailnet one. TLS is cert-manager's, through the ingress annotations.
- **Pods** run as `nobody` with a read-only root file system; `/tmp` is an `emptyDir` for the
  heartbeat file and voice-note conversion.
- **BlueBubbles** is reached at `BB_BASE_URL` over the tailnet; the cluster needs a route to it
  (the Tailscale operator or a subnet router). Nothing on the Mac is public.
- **Letta**, if switched on with `config.AGENT_RUNTIME: letta`, makes the chart give the worker a
  port and a Service and set `INTERNAL_BASE_URL` and `WORKER_INTERNAL_URL` to the two in-cluster
  addresses Letta's tools call back to.

Backups are not in the chart. Take `pg_dump` or CloudNativePG backups of the database; media in
the bucket is covered by the bucket's own versioning or lifecycle. The System page's export is a
readable copy of one household, not a restore format: there is no import.

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
- The worker logs `worker_heartbeat` every minute and restarts a crashed job after 5 seconds. Each
  heartbeat also touches `WORKER_HEARTBEAT_FILE` (default `/tmp/worker-heartbeat`), which is what a
  liveness probe should watch, and moves a `worker_heartbeat` row in `job_runs`, which is what the
  dashboard's System page reads. A database outage logs `worker_heartbeat_not_recorded` and does
  not stop the file being touched.

## Worker jobs

`python -m app.worker.main` runs `inbound`, `outbox`, `fire_reminders` (every 15 s),
`expand_recurrence` (hourly), `daily_brief`, `weekly_digest`, `consumption_model` and
`low_stock_prompt` (checked every minute), when a media backend is configured
`media_cleanup` (hourly), and when iMessage is set up `imessage_health` (every 5 minutes). What each
does and why running it twice is harmless is in
[architecture.md](architecture.md#scheduled-jobs). Log events worth watching:
`reminder_queued`, `digest_queued`, `outbox_sent`, `outbox_held_for_quiet_hours`,
`outbox_send_failed`, `outbox_delivery_failed`, `outbox_fallback_queued`, `group_created`,
`group_not_created`, `media_failed`, `photo_unreadable`, `media_cleaned`, `media_cleanup_failed`,
`invite_redeemed`, `imessage_health_changed`, `items_categorised`, `categorise_failed`, `categorise_unreadable`, `job_crashed`.

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

## iMessage

Setup is in [channels.md](channels.md#imessage). The BlueBubbles password travels as a query
parameter on every call to the Mac, so it is in the Mac's own BlueBubbles log; it is never in
this service's logs or error messages, and `httpx` request logging is off.

### BlueBubbles outage

iMessage depends on one Mac. The worker pings it every five minutes; a failed ping logs
`imessage_health_changed` with `healthy: false`, DMs the admin once ("iMessage is not
reachable..."), and shows "not reachable since" on the Channels page. From then on people are
reached on their next channel ([what moves and what does not](channels.md#when-bluebubbles-does-not-answer)).
Messages people send over iMessage in the meantime are not lost if the Mac is merely cut off from
the cluster: they stay in Messages on the Mac, but BlueBubbles does not send their webhooks again,
so anything written during the outage has to be said again.

Work through these in order; stop at the first that fixes it.

1. **Is the Mac on and awake?** Screen-share or look at it. Energy settings must never sleep the
   machine. After a power cut it must start by itself (System Settings, Energy, "Start up
   automatically after a power failure") and log in automatically, or BlueBubbles does not start.
2. **Is it on the tailnet?** `tailscale ping <mac>` from another device. An expired Tailscale key
   on the Mac looks exactly like the Mac being off: disable key expiry for it.
3. **Is BlueBubbles running?** Open the app on the Mac. From any tailnet machine:

   ```sh
   curl "$BB_BASE_URL/api/v1/ping?password=$BB_PASSWORD"     # {"status":200,"message":"Ping received!","data":"pong"}
   ```

   A 401 means `BB_PASSWORD` no longer matches the server's password. Set it and restart both
   processes.
4. **Is Messages signed in?** After a macOS update or an Apple ID password change, Messages on the
   Mac can sign out while BlueBubbles still answers pings. Then pings pass, sends fail with
   `imessage send failed: HTTP 500`, and after five tries each send moves to the person's next
   channel. Sign in again in Messages, Settings, iMessage.
5. **Do webhooks still arrive?** If sends work and nothing comes in, check the webhook in
   BlueBubbles (API and Webhooks): the URL, its `secret`, and the two events. A wrong secret
   shows as `401` for `POST /webhooks/imessage?secret=…` in the api's access log.
6. **After a BlueBubbles update** re-check step 5, then send one message each way. The adapter
   was written against server v1.9.9 and field names shift between releases.

When the next ping succeeds the worker logs `imessage_health_changed` with `healthy: true`, the
Channels page clears, and DMs go back to iMessage with nothing to undo. Sends that failed for
good during the outage are on the Activity page with "Retry".

```sql
-- since when iMessage has been unreachable, per household (no row: it is up)
select household_id, sent_at from nudge_log where dedupe_key = 'imessage_outage';
-- what was sent on another channel because of it
select created_at, channel_used, left(text, 40) from outbox o join members m on m.id = o.member_id
where m.preferred_channel = 'imessage' and o.channel_used <> 'imessage' order by created_at desc limit 20;
```

The brief time is `households.digest_time` and quiet hours are `members.quiet_start` and
`quiet_end`, all in household time. Change them on the dashboard Settings page or by telling the
assistant ("make the morning brief 7", "don't message me after 9pm").

## Presence

Phone setup is in [presence.md](presence.md). The endpoint answers 204 to everything, so the
logs are where a Shortcut is debugged. The api logs one line per call:

| Event | Meaning |
| --- | --- |
| `presence` | A ping was stored. `place_kind` and `rule` say what it set off (`store_arrival`, `out_and_about`, `both_home`, or nothing) |
| `presence_ignored` | `reason` is `unknown token` (wrong, or replaced on Settings) or `bad body` (not JSON, no `event` and `place`, an event that is not `enter` or `exit`, a place name over 80 characters) |
| `presence_rate_limited` | More than 30 pings from one link in an hour |
| `presence_failed` | An error while handling a valid ping, with the traceback |

```sql
-- what the phones have said lately
select m.name, p.name as place, p.kind, e.event, e.occurred_at from presence_events e
join members m on m.id = e.member_id join places p on p.id = e.place_id order by e.occurred_at desc limit 20;
-- the last time each nudge went out
select dedupe_key, sent_at from nudge_log order by sent_at desc limit 20;
-- what the model has learned, and what it expects to run out
select i.canonical_name, p.samples, round(p.avg_days_to_finish, 1) as days, p.last_restocked_at, p.predicted_runout_at
from consumption_profiles p join items i on i.id = p.item_id order by p.predicted_runout_at nulls last;
```

**Rotating a presence link.** Settings, "Arriving at the shops", "Replace link" next to the
person. The old link stops working at once; put the new one into each automation on that phone.
Do this if a link was pasted somewhere it should not have been. A list arriving for a shop nobody
is in is the sign of a leaked link.

**A shop sends no list.** In order: the place's kind on Settings must be `store` (a name a phone
sent first shows up as `other`); the `place` in the Shortcut must be that name; something must be
on the list for that shop; and no list for that shop may have gone to that person in the last two
hours.

## Logs

JSON lines via structlog, carrying `household_id` and `message_id` where known and never message
text. `httpx` request logging is silenced because the Telegram URL contains the bot token. The
WhatsApp token travels in a header and is never in a URL or an error message.

uvicorn's access log prints each request's path. Presence, login and calendar tokens are part of
their paths, and the setup token is a query value, so those parts are masked before the line is
written (`POST /presence/… HTTP/1.1`). An ingress or proxy in front of the api keeps its own
access log: mask or drop the path there too.

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

## Token rotation

The calendar feed token is rotated from the Calendar page, a presence link from Settings, and an
invite is replaced or revoked on the Family page. "Log out everywhere" ends every dashboard
session of a member. Provider tokens, webhook secrets, `SESSION_SECRET` and
`INTERNAL_TOOL_TOKEN` are changed in the Secret and take effect when both processes restart;
changing `SESSION_SECRET` logs everyone out.

## Not covered

A restore drill has not been done, and there is no import for the System page's export.
