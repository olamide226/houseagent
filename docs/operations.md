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
| `LLM_PROVIDER` | yes | | `openai_compat` or `anthropic`, with a key; `claude_code` or `codex_cli`, on a subscription ([below](#subscription-providers)) |
| `LLM_BASE_URL` | for `openai_compat` | | Endpoint; optional override for `anthropic`. Not read by the subscription providers |
| `LLM_API_KEY` | yes (except local models and the subscription providers) | | Provider key |
| `LLM_MODEL` | yes | | Must support tool calling. For a subscription provider, a name its CLI takes (`haiku`, `gpt-5.6-luna`) |
| `LLM_FAST_MODEL` | no | `LLM_MODEL` | A cheaper model for the nightly job that sorts items into categories |
| `LLM_SUPPORTS_IMAGES` | no | `true` | `false`: photos are not sent to the model and the agent says it cannot read them |
| `LLM_MAX_TOOL_ITERATIONS` | no | `8` | Loop guard |
| `LLM_CLI_PATH` | no | `claude` or `codex` on `PATH` | Subscription providers: where the CLI is |
| `LLM_CLI_TIMEOUT` | no | `120` | Subscription providers: seconds one model step may take before its CLI is stopped |
| `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CONFIG_DIR`, `CODEX_HOME` | no | | Not read by the app: passed to the CLI, which reads them ([below](#subscription-providers)) |
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
| `INTERNAL_BASE_URL` | no | `PUBLIC_BASE_URL` | Where Letta reaches the api's `/internal` (Practice chat turns) |
| `WORKER_INTERNAL_URL` | no | `http://localhost:8001` | Where Letta reaches the worker's `/internal` (chat turns); the worker listens on this port under `letta` |
| `AGENT_NAME` | no | `Home` | Name used in the prompt and pages |
| `PRESENCE_SHORTCUT_URL` | no | | The iCloud link of the Shortcut the admin shared once. With it, the page a presence link opens gives everyone a "Get the shortcut" button ([presence.md](presence.md#sharing-the-shortcut-once)) |
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

The database lives in the volume `pgdata` and has no published port. The database, the api and the
worker restart by themselves, and `docker compose ps` shows the health of each: the api's is
`/readyz`, the worker's is its heartbeat file. Compose reads four variables of its own from the
same `.env`:

| Variable | Default | Purpose |
| --- | --- | --- |
| `API_PUBLISH` | `8000` | Where the api is published: a port, or `address:port` to open it on one address only |
| `POSTGRES_PASSWORD` | `ha` | The database's password. It is fixed when the volume is first created |
| `CLAUDE_CODE_VERSION`, `CODEX_VERSION` | empty | Builds that CLI into the image ([below](#the-cli-in-the-image)) |

## On one server

One household fits on one machine: the same compose file, behind whatever ends TLS there.
`https://houseagent.devng.host` runs this way since 9 Oct 2026, with a cluster's ingress in front
of it.

### Deploy

```sh
git clone https://github.com/olamide226/houseagent.git && cd houseagent
cp .env.example .env && chmod 600 .env
```

Fill `.env` in as on a laptop, and add:

```sh
PUBLIC_BASE_URL=https://houseagent.devng.host
POSTGRES_PASSWORD=...               # openssl rand -hex 24: letters and digits only, it goes into a URL
API_PUBLISH=172.17.0.1:18090        # an address the proxy reaches and the internet does not
```

```sh
docker compose up -d --build
docker compose ps                   # db, api and worker healthy; migrate exited (0)
```

Then point the proxy at that port, and Telegram at the new address
([channels.md](channels.md#setup-checklist)).

**The port is the whole api.** Whoever reaches it also reaches `/readyz` and `/internal`, which
the proxy is there to keep in. Docker opens a published port in the firewall itself, past `ufw`,
so a bare port number is open to the internet. Publish on loopback when the proxy runs on the
machine, or on the Docker bridge address (`172.17.0.1`) when it runs in a cluster on it.

**The dev server.** `deploy/k8s/dev/` is three plain manifests for the k3s cluster that the dev
server is a node of: a Service with no selector, an EndpointSlice written by hand to
`172.17.0.1:18090`, and an Ingress for `houseagent.devng.host` with a certificate from
cert-manager. The Ingress routes the same paths as the chart ([below](#deploying-with-helm)) and
nothing else. Its proxy keeps an access log with full paths, so login, presence and calendar
tokens are in that log.

```sh
sudo k3s kubectl apply -f deploy/k8s/dev/
sudo k3s kubectl -n dev get certificate houseagent-devng-tls      # READY True after about half a minute
curl -i https://houseagent.devng.host/healthz                     # 200; /readyz is 404 from the proxy
```

That server also has a `docker-compose.override.yml` beside the compose file. It is not in the
repository (`.gitignore` names it) and holds only what is that machine's own: which cgroup the
containers run in.

### Update and roll back

```sh
git pull && docker compose up -d --build      # builds, migrates, then replaces the api and the worker
```

The api is away for a few seconds; Telegram and WhatsApp send again what was not taken. To go
back, `git checkout <commit>` and run the same command. That does not undo a migration.

### Logs

```sh
docker compose logs -f --tail=100 api worker
```

### Backup and restore

```sh
docker compose exec -T db pg_dump -U ha -Fc houseagent > houseagent-$(date +%F).dump
```

Nothing does this for you: run it from cron and keep the file on another machine. To restore, into
an empty database:

```sh
docker compose stop api worker
docker compose exec -T db dropdb -U ha --force houseagent
docker compose exec -T db createdb -U ha houseagent
docker compose exec -T db pg_restore -U ha -d houseagent --no-owner --no-acl --exit-on-error < houseagent-2026-10-09.dump
docker compose up -d
```

### Moving a household from another machine

Done once, on 9 Oct 2026, from a Mac (Postgres 15.2, dumped with `pg_dump` 16.1) to this stack
(Postgres 16.15). All 22 tables matched in row count and content, and Telegram was without a
webhook that answered for 40 seconds.

1. **Rehearse while the old copy still runs.** Dump it with
   `pg_dump -Fc --no-owner --no-acl`, restore it as above, start the api alone
   (`docker compose up -d api`), compare `select count(*)` per table, and call `/readyz`. Do not
   start the worker on a copy: it would send the same reminders and briefs as the one still
   running.
2. **Stop the old api, then the old worker** once it has nothing in hand, so no reply is cut off
   half way. Both of these are 0 when it is idle:

   ```sql
   select count(*) from messages where direction = 'in' and status in ('received', 'processing');
   select count(*) from outbox where status = 'sending' or (status = 'pending' and send_after <= now());
   ```

3. **Dump again, restore into an empty database, compare again**, then `docker compose up -d`.
4. **Point Telegram at the new address** with `setWebhook` as in
   [channels.md](channels.md#setup-checklist): the same secret, and no `drop_pending_updates`.
   Telegram kept what was sent meanwhile and delivers it now. `getWebhookInfo` must show the new
   address and no `last_error_message`.
5. **Links people already have keep the old address.** A personal link for the shops is replaced on
   Settings ("Replace link") and put into the phone again; a calendar feed is subscribed to again
   from the Calendar page. Login links are made new each time.

Keep the old database until the new one has run for a while. To go back before then: start the old
api and worker and run `setWebhook` with the old address.

### The Claude subscription on a server

The stack can answer on a Claude subscription instead of an API key
([Subscription providers](#subscription-providers); read what the vendors' terms say first). With
`CLAUDE_CODE_VERSION=2.1.292` in `.env` the image has the CLI. The containers run as `nobody`, who
has no home, so `.env` also needs `HOME=/tmp` and `CLAUDE_CONFIG_DIR=/tmp/claude`. The switch is
then three lines and `docker compose up -d`:

```sh
LLM_PROVIDER=claude_code
LLM_MODEL=claude-sonnet-5-5
CLAUDE_CODE_OAUTH_TOKEN=...         # printed by `claude setup-token` on a machine with a browser
```

On the dev server the image and the two paths are in place and the three lines are there,
commented out. Without the token the adapter answers "Claude Code is not signed in"; with a token
it has not been tried.

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
- **Ingress:** only these paths reach the api: `/webhooks`, `/presence`, `/ics`, `/healthz`,
  `/static`, and for the dashboard `/setup`, `/login`, `/logout`, `/dashboard`. `/internal` and
  `/readyz` are never routed. With `dashboard.public: false` the dashboard paths are left off
  that ingress (`/static` stays, for the page a presence link opens), and `dashboard.internalIngress` can serve them on another ingress class, such as
  a tailnet one. TLS is cert-manager's, through the ingress annotations.
- **Pods** run as `nobody` with a read-only root file system; `/tmp` is an `emptyDir` for the
  heartbeat file and voice-note conversion.
- **A subscription provider** needs the CLI in the image, and for Codex the claim named by
  `codexHome.existingClaim`: see [Subscription providers](#subscription-providers).
- **BlueBubbles** is reached at `BB_BASE_URL` over the tailnet; the cluster needs a route to it
  (the Tailscale operator or a subnet router). Nothing on the Mac is public.
- **Letta**, if switched on with `config.AGENT_RUNTIME: letta`, makes the chart give the worker a
  port and a Service and set `INTERNAL_BASE_URL` and `WORKER_INTERNAL_URL` to the two in-cluster
  addresses Letta's tools call back to.

Backups are not in the chart. Take `pg_dump` or CloudNativePG backups of the database; media in
the bucket is covered by the bucket's own versioning or lifecycle. The System page's export is a
readable copy of one household, not a restore format: there is no import.

## Subscription providers

`LLM_PROVIDER=claude_code` runs the model on a Claude subscription and `codex_cli` on a ChatGPT
plan, through the vendor's own CLI ([llm.md](llm.md#subscription-providers) has how, and what the
vendors' terms say: read that before switching). The api and the worker each start the CLI
themselves, so wherever they run needs three things: the CLI, its sign-in, and somewhere the CLI
may write.

**They were tried on a laptop, never deployed.** On the laptop both ran the eval suite on the CLI's
existing sign-in. In the container image both CLIs start as `nobody` on a read-only root and the
adapters report that nobody is signed in; a signed-in container was not tried, and nor was the
chart on a cluster.

### On your own machine

Install the CLI, sign in once, and set two variables. Tested with Claude Code 2.1.292 and Codex
CLI 0.154.0.

```sh
claude            # then /login, with the subscription account
LLM_PROVIDER=claude_code  LLM_MODEL=haiku

codex login       # "Sign in with ChatGPT"
LLM_PROVIDER=codex_cli    LLM_MODEL=gpt-5.6-luna
```

Leave `LLM_BASE_URL` and `LLM_API_KEY` unset. The model names are the CLI's: `claude --model`
takes `haiku`, `sonnet` or a full model id, and `codex` takes what your plan offers (a model it
does not offer fails with "... is not supported when using Codex with a ChatGPT account").
Your own settings, memory, hooks, skills and MCP servers are not used by these runs, and the runs
leave no session behind.

### The CLI in the image

The `Dockerfile` installs neither by default. Name a version to add one or both; each is a single
file in `/usr/local/bin`, about 250 MB and 230 MB:

```sh
docker build --build-arg CLAUDE_CODE_VERSION=2.1.292 --build-arg CODEX_VERSION=0.154.0 -t household-agent .
```

Claude Code comes from Anthropic's installer (`claude.ai/install.sh`) and Codex from its GitHub
release. Neither updates itself in the image. One run of `claude` peaks near 240 MB of memory and
one of `codex` near 170 MB, and up to two run at once in each process, so raise the pods' memory
limits (`api.resources.limits.memory`, `worker.resources.limits.memory`) by about 500 MB.

### The sign-in in a container

The pods run as `nobody` with a read-only root, so the CLI needs a home it can write. `/tmp` is
already writable:

```yaml
config:
  HOME: /tmp
```

**Claude.** Make a token on any machine with a browser and put it in the Secret:

```sh
claude setup-token        # prints a one-year token for the subscription; it can only make model requests
```

```yaml
# in the existing Secret: CLAUDE_CODE_OAUTH_TOKEN: <the token>
config:
  LLM_PROVIDER: claude_code
  LLM_MODEL: haiku
  HOME: /tmp
  CLAUDE_CONFIG_DIR: /tmp/claude
```

Nothing has to persist: the token is the whole sign-in, and both pods can use it at once.

**Codex.** Its sign-in is a file, `$CODEX_HOME/auth.json`, that Codex **rewrites** each time it
refreshes the session (about every eight days). So it lives on a volume, not in a Secret, and the
api and the worker must share the one copy: OpenAI's guide says "Do not share the same file
across concurrent jobs or multiple machines", because the copy that refreshes first logs the
other out. Create a PersistentVolumeClaim that both pods can mount (`ReadWriteMany`, or
`ReadWriteOnce` with both pods on one node) and name it:

```yaml
codexHome:
  existingClaim: codex-sign-in      # mounted at /codex in both pods, and set as CODEX_HOME
config:
  LLM_PROVIDER: codex_cli
  LLM_MODEL: gpt-5.6-luna
  HOME: /tmp
```

Then sign in on that volume, once, from inside a pod:

```sh
kubectl exec -it deploy/home-worker -- codex login --device-auth    # open the link, enter the code
kubectl exec deploy/home-worker -- codex login status               # "Logged in using ChatGPT"
```

That is a sign-in of its own, so your laptop's stays as it is. Do not copy your laptop's
`auth.json` into the volume: the two would then take turns logging each other out. Treat the
volume like a password. Device-code sign-in has to be allowed in your ChatGPT security settings.

With Docker Compose the same two things are a named volume mounted at `/codex` in `api` and
`worker` with `CODEX_HOME=/codex`, or `CLAUDE_CODE_OAUTH_TOKEN` in `.env`
([The Claude subscription on a server](#the-claude-subscription-on-a-server)).

### When the sign-in expires or the allowance runs out

Nothing checks the sign-in ahead of time. The first turn after it lapses fails: the person gets
the usual apology, the message is marked failed on Activity, and the System page's Model card
says why until a later message is answered:

| The System page says | What happened | Do |
| --- | --- | --- |
| `Claude Code is not signed in, or its sign-in has expired: ...` | The token is missing, a year old, or was revoked | `claude setup-token` again, replace `CLAUDE_CODE_OAUTH_TOKEN` in the Secret, restart both pods |
| `Codex is not signed in, or its sign-in has expired: ...` | `auth.json` is missing, its refresh token was revoked, or another copy refreshed first | `codex login --device-auth` in a pod again |
| `... has reached the subscription's usage limit: ...` | The plan's five-hour or weekly allowance is used up, by the household or by you | Wait for the reset the message names, use a smaller model, or switch `LLM_PROVIDER` back to an API key |
| `... could not be started (FileNotFoundError) ...` | The CLI is not in the image or not on `PATH` | Rebuild with the build argument, or set `LLM_CLI_PATH` |
| `... gave no answer within 120 seconds` | The vendor was slow or the CLI hung | It was stopped; raise `LLM_CLI_TIMEOUT` if it keeps happening |
| `... ran with other tools than the household's` or `... used a tool of its own` | A CLI version that no longer honours the flags that switch its own tools off | The answer was discarded. Go back to a tested version |

Messages that failed are not retried; the person has to say it again. To check by hand:
`kubectl exec deploy/home-worker -- codex login status`, or for Claude send any message in
Practice chat.

The allowance is shared with your own use of Claude or ChatGPT, and each vendor counts it in its
own way ([llm.md](llm.md#limits)). A household turn is two or three model steps.

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

Setup and the template are in [channels.md](channels.md#whatsapp). The Chat apps page shows, per
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
5. Failed sends are on the Activity page with "Send again". A retry within 24 hours of the person
   writing goes out as an ordinary message.

### An expired or revoked WhatsApp token

Every WhatsApp call fails with Graph error `190`. Sends fail at once and move to each member's
next channel, incoming photos and voice notes cannot be downloaded (the turn still runs and says
so), and group creation shows the error. Set a new `WA_ACCESS_TOKEN` and restart both processes.
Incoming text keeps working throughout, because webhooks are checked with the app secret.

## iMessage

Setup is in [channels.md](channels.md#imessage). The BlueBubbles password travels as a query
parameter on every call to the Mac, because BlueBubbles takes it no other way. Treat anything
between the cluster and the Mac that logs URLs as holding it. It is never in this service's logs
or error messages, and `httpx` request logging is off.

### BlueBubbles outage

iMessage depends on one Mac. The worker pings it every five minutes; a failed ping logs
`imessage_health_changed` with `healthy: false`, DMs the admin once ("iMessage is not
reachable..."), and shows "Not reachable since" on the Chat apps page. From then on people are
reached on their next channel ([what moves and what does not](channels.md#when-bluebubbles-does-not-answer)).
**BlueBubbles does not retry a webhook.** It posts each one once and only logs a failure
(`webhookService` in its source). So a message someone sends over iMessage while the Mac cannot
reach the api, or while the api is down, is never seen by the assistant, although it sits in
Messages on the Mac. It has to be said again. Telegram and WhatsApp do retry.

This runbook is written from the BlueBubbles server source and from what running a Mac as a
server usually involves. No outage has happened yet, so none of it has been exercised.

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
Chat apps page clears, and DMs go back to iMessage with nothing to undo. Sends that failed for
good during the outage are on the Activity page with "Send again".

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

Phone setup is in [presence.md](presence.md). A POST is answered 204 whatever it holds, so the
logs are where a Shortcut is debugged. Opening a link in a browser is a GET: it shows a page and
logs nothing here. The api logs one line per call:

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

**Rotating a presence link.** Settings, "The list when you reach a shop", "Replace link" next to the
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

Each turn's token usage is in `messages.meta.usage` and, for an admin, under "Technical details" on
the Activity page:

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
changing `SESSION_SECRET` logs everyone out. `CLAUDE_CODE_OAUTH_TOKEN` lasts a year and is
replaced the same way; Codex's sign-in is renewed with `codex login` on its volume
([Subscription providers](#subscription-providers)).

## Not covered

A restore has been done once, when the household moved to the dev server
([above](#moving-a-household-from-another-machine)); it is not practised on a schedule and no
backup is taken automatically. There is no import for the System page's export. The subscription
providers have not run in a signed-in container or on a cluster.
