# Household Agent: Implementation Spec v1

Oct 5, 2026 · @Olamide

## 1. Overview

v1 is one FastAPI service, one worker process and one Postgres database. Two adults manage food stock, a shared shopping list, appointments and reminders by talking to an agent called Home (configurable via `AGENT_NAME`) over Telegram, WhatsApp or iMessage, in DMs or a family group.

**Goals**

- Zero-form capture: text, voice notes, receipt and fridge photos, forwarded messages.
- A shared shopping list that replaces the market board.
- Appointments and recurring activities (e.g. Chatterbox) with reminders.
- Store-arrival nudges triggered by iOS Shortcuts, with no custom app.
- A small web dashboard for setup, oversight and debugging. No CLI.
- Channel-agnostic: a new channel is one adapter module plus identity rows.
- Model-agnostic: any tool-calling LLM through a provider adapter; no vendor SDK types outside `app/llm/`.
- Runtime-agnostic: a plain tool-calling loop ships first; Letta plugs in behind the same interface.

**Non-goals for v1**

- Multi-household signup or billing (the schema is multi-household ready).
- A native mobile app.
- Barcode scanning, price tracking, meal planning.

**Design principles**

1. Postgres is the source of truth. The agent only reads and writes through tools.
2. LLM for language, code for rules. Anything deterministic (finished staple to the list, bought to restocked, store arrival to the list) is code, not prompt.
3. Approximate beats absent. Quantities are optional everywhere.
4. Quiet by default. A reaction instead of a reply; one digest instead of five pings.
5. Idempotent everywhere. Every webhook may arrive twice; every job may run twice.
6. Everything is undoable. "Undo" reverts the last action, including stock.
7. Nothing leaves the household. The router can only send to handles in `channel_identities`.

## 2. Architecture and repo layout

One container image runs as two processes: `api` (uvicorn, stateless; receives webhooks, serves the dashboard, returns 200 fast) and `worker` (one replica, does all slow work). They communicate only through Postgres tables (`messages`, `outbox`, `reminders`) plus `LISTEN/NOTIFY` for low latency. Media goes through a pluggable store: an S3-compatible bucket or ImgBB.

&#91;embedded content: system architecture · 2 processes, 1 database\]

Channels and Shortcuts only ever talk to the api process; everything that takes time, calls a model or sends a message happens in the worker, coordinated through Postgres.

**Stack decisions**

- Python 3.12, FastAPI, Pydantic v2, pydantic-settings.
- SQLAlchemy 2.x async Core (no ORM) on asyncpg; Alembic for migrations; migration `0001` is `schema.sql` verbatim.
- LLM access only through `app/llm/` (section 8.1). Ships with an OpenAI-compatible adapter (OpenAI, Azure OpenAI, OpenRouter, Groq, Together, Ollama, vLLM) and an Anthropic adapter; chosen by `LLM_PROVIDER`.
- Dashboard: Jinja2 templates plus HTMX, served by the api process. No frontend build, no second deployable. If it outgrows this, a Next.js app can replace it later because it only calls the same internal services.
- Media through a `MediaStore` interface with `s3` and `imgbb` backends (section 7.4).
- `python-dateutil` for RRULE expansion; `zoneinfo` for time zones.
- `ffmpeg` in the image for audio conversion (iMessage voice notes arrive as `.caf`).
- `structlog` JSON logs; every log line carries `household_id`, `message_id` where known.

**Repo layout**

```text
household-agent/
  pyproject.toml            # uv-managed
  Dockerfile                # python:3.12-slim + ffmpeg
  alembic.ini
  schema.sql
  migrations/versions/0001_initial.py   # executes schema.sql
  docs/                     # section 18
    README.md  architecture.md  data-model.md  channels.md  agent-and-tools.md
    dashboard.md  llm.md  operations.md  evals.md  spec.md
    adr/0001-postgres-source-of-truth.md ...
    diagrams/architecture.mmd
  app/
    main.py                 # create_app(): routers, lifespan (db pool, adapters, llm)
    config.py               # Settings (pydantic-settings)
    db.py                   # engine, tx() context manager, advisory lock helper
    core/
      envelope.py           # InboundEvent, Envelope, OutboundMessage, Capabilities
      identity.py           # handle -> member, invite code redemption
      timeutil.py           # household tz, quiet hours, rrule helpers
    llm/
      types.py              # neutral ChatMessage, ToolDef, ToolCall, LLMResponse
      base.py               # LLMClient protocol + factory
      openai_compat.py      # OpenAI-compatible chat completions
      anthropic.py          # Anthropic Messages
      stt.py                # SpeechToText protocol + adapters
    media/
      store.py              # MediaStore protocol + factory
      s3.py  imgbb.py
    channels/
      base.py               # ChannelAdapter protocol + registry
      telegram.py  whatsapp.py  imessage.py
    pipeline/
      inbound.py            # persist, dedupe, debounce, dispatch to agent
      media.py              # fetch, store, transcribe
      router.py             # outbox dispatcher, channel choice, fallback
    agent/
      base.py               # AgentRuntime protocol, AgentResult
      loop.py               # plain tool-calling loop (default)
      letta_runtime.py      # optional, milestone 6
      prompt.py             # system prompt + household brief builder
      resolve.py            # item / member / location / event resolution
      actions.py            # agent_actions log + undo machinery
      stock.py              # pure stock transition function
      tools/
        __init__.py         # REGISTRY: name -> ToolSpec
        inventory.py  shopping.py  calendar.py  memory.py  family.py  undo.py  onboarding.py
    services/               # shared by tools AND dashboard (one write path)
      inventory.py  shopping.py  calendar.py  members.py  households.py
    dashboard/
      routes.py             # pages + HTMX partials
      auth.py               # magic links, signed session cookie, CSRF
      templates/            # Jinja2
      static/               # htmx.min.js, one CSS file
    worker/
      main.py               # asyncio supervisor for all jobs
      jobs.py               # reminders, recurrence, predictions, digests, health
    presence/
      routes.py  rules.py
    ics/
      routes.py             # read-only calendar feed
  tests/
    unit/  contract/  evals/
  deploy/helm/household-agent/
```

Tools and dashboard pages never write SQL themselves; both call `app/services/`, so an edit made in the browser goes through the same stock rules, undo log and outbox as one made by voice note.

## 3. Configuration and dependencies

All configuration is environment variables read by one `Settings` class; a channel is enabled only when its variables are set, so a deployment can run Telegram alone.

**Dependencies** (`pyproject.toml`): `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pydantic-settings`, `sqlalchemy[asyncio]>=2`, `asyncpg`, `alembic`, `httpx`, `jinja2`, `itsdangerous`, `python-multipart`, `qrcode`, `python-dateutil`, `inflect`, `structlog`, `tenacity`, `aioboto3`. LLM extras, installed per provider: `openai` (extra `llm-openai`), `anthropic` (extra `llm-anthropic`). Dev: `pytest`, `pytest-asyncio`, `respx`, `testcontainers[postgres]`, `freezegun`. Optional: `letta-client`.

| Variable | Required | Example / default | Purpose |
| --- | --- | --- | --- |
| `DATABASE_URL` | yes | `postgresql+asyncpg://ha:...@db/ha` | Postgres 15+ |
| `PUBLIC_BASE_URL` | yes | `https://home.example.com` | Webhook, presence, ICS and login links |
| `AGENT_NAME` | no | `Home` | Name used in prompts and templates |
| `AGENT_RUNTIME` | no | `loop` | `loop` or `letta` |
| `LLM_PROVIDER` | yes | `openai_compat` | `openai_compat` or `anthropic` |
| `LLM_BASE_URL` | for `openai_compat` | `https://openrouter.ai/api/v1`, `http://ollama:11434/v1` | Any OpenAI-compatible endpoint |
| `LLM_API_KEY` | yes (except local) |  | Provider key |
| `LLM_MODEL` | yes | provider's model id | Main agent model; must support tool calling |
| `LLM_FAST_MODEL` | no | `LLM_MODEL` | Cheap model for nightly categorisation |
| `LLM_SUPPORTS_IMAGES` | no | `true` | If false, photos get a polite "can't read photos with this model" reply |
| `LLM_MAX_TOOL_ITERATIONS` | no | `8` | Loop guard |
| `STT_PROVIDER` | yes | `openai_compat` | `openai_compat` or `local` (faster-whisper sidecar) |
| `STT_BASE_URL` / `STT_API_KEY` / `STT_MODEL` | per provider |  | Transcription |
| `MEDIA_BACKEND` | no | `s3` | `s3` or `imgbb` |
| `S3_ENDPOINT` / `S3_BUCKET` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` | for `s3` | MinIO in cluster | Media storage |
| `IMGBB_API_KEY` | for `imgbb` |  | Image hosting |
| `MEDIA_RETENTION_DAYS` | no | `90` | Auto-delete; ImgBB caps this at 180 |
| `DEBOUNCE_SECONDS` | no | `4` | Batch rapid consecutive messages |
| `TG_BOT_TOKEN` / `TG_BOT_USERNAME` / `TG_WEBHOOK_SECRET` | for Telegram |  | Bot API; username builds invite deep links |
| `WA_PHONE_NUMBER_ID` / `WA_ACCESS_TOKEN` / `WA_APP_SECRET` / `WA_VERIFY_TOKEN` / `WA_API_VERSION` | for WhatsApp | `v23.0` | Cloud API |
| `WA_REMINDER_TEMPLATE` | for WhatsApp | `household_reminder` | Utility template for out-of-window sends |
| `BB_BASE_URL` / `BB_PASSWORD` / `BB_WEBHOOK_SECRET` | for iMessage | `http://mac-mini.tailnet:1234` | BlueBubbles server |
| `BB_PRIVATE_API` | no | `false` | Enables tapbacks and threaded replies |
| `SESSION_SECRET` | yes | 64 random bytes | Signs dashboard cookies and CSRF tokens |
| `SETUP_TOKEN` | first run |  | Unlocks `/setup` until the first household exists, then ignored |
| `DASHBOARD_PUBLIC` | no | `true` | `false` serves the dashboard only on the internal or tailnet ingress |
| `INTERNAL_TOOL_TOKEN` | for Letta |  | Auth for `/internal/tools/*` |
| `LETTA_BASE_URL` / `LETTA_API_KEY` | for Letta |  | Letta server |
| `DEFAULT_TIMEZONE` | no | `Europe/London` | New households |
| `LOG_LEVEL` | no | `INFO` |  |

Check `WA_API_VERSION` and model names against current provider docs at build time; both change often.

## 4. Data model

The full DDL is the companion file `schema.sql` (v1.2), applied as migration `0001`. Postgres holds all state; the agent never touches tables directly.

| Table | Purpose | Written by |
| --- | --- | --- |
| `households` | Name, time zone, primary group thread, onboarding state, digest time, calendar token | setup page, dashboard, onboarding |
| `members` | Adults and children; preferred channel; quiet hours; presence and invite token hashes; session version | dashboard, `add_family_member` |
| `login_tokens` | One-time dashboard magic links (hashed, 10-minute expiry) | `/login` flow |
| `channel_identities` | Handle per channel per member (phone, Apple ID, Telegram user id) | invite redemption |
| `threads` | One row per DM or group chat per channel | inbound pipeline |
| `messages` | Every message in and out, with processing status; idempotency key `(thread_id, external_id)` | pipeline, router |
| `household_facts` | Durable key/value facts (brands, shops, routines) | `remember`, dashboard |
| `locations` | Fridge, freezer, store, plus any custom location | seed, `log_inventory`, dashboard |
| `items` | Canonical item, aliases, category, default location, staple flag, low threshold | resolver, dashboard |
| `inventory_events` | Append-only stock log | inventory service |
| `stock` | Current projection per item and location | same transaction as each event |
| `consumption_profiles` | Learned run-out intervals | nightly job |
| `shopping_list_items` | Active and resolved list entries; one active row per item | shopping service, rules |
| `events` | Appointments and activities, RRULE, exception dates | calendar service |
| `reminders` | Concrete fire times, one-off or RRULE | calendar service, recurrence job |
| `places` | Home, shops, school, clinic (names match Shortcut payloads) | onboarding, dashboard, presence |
| `presence_events` | Enter and exit pings | presence route |
| `nudge_log` | Dedupe for proactive nudges | rules, jobs |
| `agent_actions` | Every write with its inverse, for undo (agent and dashboard) | services |
| `outbox` | Every outbound send, with retries and quiet-hours deferral | services, jobs, pipeline |
| `job_runs` | Once-per-day guarantee for scheduled jobs | worker |

**Stock transitions** (`app/agent/stock.py`, a pure function `apply(stock_row, event, item) -> stock_row`; table-driven unit tests)

| Event | qty\_estimate | status | Side effect (in code) |
| --- | --- | --- | --- |
| `added`, `restocked` | `qty + quantity` if both known, else unchanged (null stays null) | `in_stock` | Resolve matching active list entry to `bought` |
| `used` | `max(qty - quantity, 0)` if both known, else unchanged | recomputed | none |
| `low` | unchanged | `low` | Add to shopping list, reason `low` |
| `finished` | `0` | `out` | Add to list (reason `finished`) if staple; auto-mark staple after 2 completed restock cycles |
| `discarded` | as `used`; null quantity means `finished` | recomputed | as `finished` when it reaches 0 |
| `adjusted` | `quantity` (absolute); null means unchanged | `in_stock` unless quantity is 0 | none |

Recompute rule: `out` if qty = 0; `low` if `low_threshold` set and qty <= threshold; `in_stock` if qty > 0; otherwise keep the previous status (`unknown` for a fresh row with no quantity).

**Hard rules**

- `stock` is only written inside the same transaction that appends the `inventory_events` row. A rebuild script replays events in `occurred_at` order to regenerate `stock` from scratch.
- A photo never marks anything `finished` by absence. Cameras miss things.
- Every household is seeded with locations `fridge`, `freezer`, `store`. A new item with no location goes to `store`.
- All timestamps are `timestamptz` in UTC; conversion to household time zone happens only at the edges (prompt, digests, RRULE expansion).

## 5. Core types

Three types carry every message: adapters produce `InboundEvent`, the pipeline turns it into an `Envelope` for the agent, and everything outbound is an `OutboundMessage` row in `outbox`. No module outside `app/channels/` imports a channel SDK or knows a payload shape.

```python
# app/core/envelope.py
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol
from pydantic import BaseModel
from fastapi import Request

class Channel(StrEnum):
    telegram = "telegram"
    whatsapp = "whatsapp"
    imessage = "imessage"

class MediaRef(BaseModel):
    kind: Literal["image", "audio", "video", "document", "location"]
    mime: str | None = None
    external_id: str | None = None   # provider media id / file id / attachment guid
    storage_backend: Literal["s3", "imgbb"] | None = None
    storage_key: str | None = None   # s3: {household}/{message_id}/{n}.{ext}; imgbb: image id
    storage_url: str | None = None   # imgbb only: direct URL (public to anyone with the link)
    delete_url: str | None = None    # imgbb only: deletion link for early cleanup
    transcript: str | None = None    # audio only, set by media.py
    lat: float | None = None
    lng: float | None = None
    caption: str | None = None

class InboundEvent(BaseModel):
    """Adapter output. Channel-shaped facts only, no household knowledge."""
    channel: Channel
    external_message_id: str
    external_thread_id: str
    scope: Literal["dm", "group"]
    sender_handle: str               # normalised: E.164 phone, Telegram user id, Apple ID
    sender_name: str | None = None
    text: str | None = None
    media: list[MediaRef] = []
    reply_to_external_id: str | None = None
    reaction_emoji: str | None = None        # set when the event IS a reaction
    reaction_target_external_id: str | None = None
    sent_at: datetime
    raw: dict

class Envelope(BaseModel):
    """What the agent sees. Built after identity resolution and debounce."""
    household_id: str
    member_id: str | None            # None for system turns (jobs, presence)
    member_name: str | None
    thread_id: str | None
    message_ids: list[str]           # debounced batch, oldest first
    channel: Channel | None
    scope: Literal["dm", "group"] | None
    text: str                        # joined texts + "[voice note] ..." transcripts + reaction lines
    images: list[MediaRef] = []
    reply_to_text: str | None = None
    kind: Literal["user", "system"] = "user"
    received_at: datetime

class OutboundMessage(BaseModel):
    household_id: str
    target: Literal["thread", "member", "household"]
    thread_id: str | None = None
    member_id: str | None = None
    text: str | None = None
    react_emoji: str | None = None           # "ack" means the adapter's ack emoji
    reply_to_message_id: str | None = None
    urgency: Literal["low", "normal", "high"] = "normal"
    respect_quiet_hours: bool = True
    dedupe_key: str | None = None

class Capabilities(BaseModel):
    groups: bool
    reactions: bool
    ack_emoji: str                   # Telegram: "\U0001F44D", WhatsApp/iMessage: "\u2705"
    voice_in: bool
    images_in: bool
    threaded_replies: bool
    proactive_window_hours: int | None   # 24 for WhatsApp, None = unlimited
    max_text_len: int                    # 4096 Telegram/WhatsApp, 10000 iMessage
    formatting: Literal["plain", "whatsapp", "telegram_html"]

class SendResult(BaseModel):
    external_id: str | None

class ChannelAdapter(Protocol):
    channel: Channel
    capabilities: Capabilities
    async def verify(self, request: Request, body: bytes) -> None: ...   # raise HTTPException(401)
    async def parse(self, body: bytes) -> list[InboundEvent]: ...
    async def fetch_media(self, ref: MediaRef) -> tuple[bytes, str]: ...   # (bytes, mime)
    async def send_text(self, external_thread_id: str, text: str,
                        reply_to_external_id: str | None = None) -> SendResult: ...
    async def react(self, external_thread_id: str, external_message_id: str, emoji: str) -> None: ...
    async def send_template(self, external_thread_id: str, name: str, params: list[str]) -> SendResult: ...
    def dm_thread_id(self, handle: str) -> str: ...   # external thread id for a DM with this handle
    def format(self, text: str) -> str: ...          # *bold* etc. per platform
```

Adapters that cannot do something raise `NotSupported`; the router catches it and degrades (a text "\\U0001F44D" instead of a reaction, a plain send instead of a template). Adapters register in `channels/base.py` as `ADAPTERS: dict[Channel, ChannelAdapter]`, built at startup only for channels whose env vars are present.

## 6. Channel adapters

Build Telegram first: it is free, needs no business verification, and supports groups, voice and reactions natively. WhatsApp and iMessage follow behind the same contract test suite. Every adapter ignores messages sent by its own bot identity to prevent loops.

### 6.1 Telegram (Bot API)

- **Setup:** create the bot with BotFather; run `/setprivacy` then `Disable` so the bot sees every message in the household group, not only commands. Call `setWebhook` with `url={PUBLIC_BASE_URL}/webhooks/telegram`, `secret_token=TG_WEBHOOK_SECRET`, `allowed_updates=["message","edited_message","message_reaction"]`. Make the bot a group admin so reaction updates arrive.
- **Verify:** header `X-Telegram-Bot-Api-Secret-Token` equals `TG_WEBHOOK_SECRET` (constant-time compare).
- **Parse:** `update.message`: `chat.id` is the thread (`chat.type` `private` means `dm`; `group` or `supergroup` means `group`); `from.id` is the handle; `text` or `caption`; `voice.file_id` (OGG/Opus) is audio; `photo[-1].file_id` (largest size) is an image; `location` gives lat/lng; `reply_to_message.message_id` sets reply. `update.message_reaction` gives `new_reaction[0].emoji` and `message_id`. `edited_message` is ignored in v1.
- **Media:** `getFile(file_id)` returns `file_path`; download `https://api.telegram.org/file/bot{token}/{file_path}`.
- **Send:** `sendMessage` with `chat_id`, `text`, `parse_mode=HTML`, `reply_parameters={message_id}`. React with `setMessageReaction` (`reaction=[{type:"emoji", emoji:"\U0001F44D"}]`); Telegram only accepts a fixed emoji set, hence `ack_emoji` per adapter.
- **DM thread id:** a private chat id equals the user id, but only after the user has pressed Start once. Invites use the deep link https://t.me/{TG\_BOT\_USERNAME}?start={code}, so tapping it presses Start and redeems the code in one go (the adapter treats "/start CODE" as the code).

### 6.2 WhatsApp (Cloud API)

- **Setup:** dedicated number (not on the WhatsApp Business app, since Coexistence numbers cannot use the Groups API). Subscribe the app to the `messages` webhook field. Submit the utility template `household_reminder` with body `Reminder from Home: {{1}}`.
- **Verify (GET):** if `hub.mode == "subscribe"` and `hub.verify_token == WA_VERIFY_TOKEN`, return `hub.challenge` as plain text.
- **Verify (POST):** header `X-Hub-Signature-256` equals `"sha256=" + hex(HMAC_SHA256(WA_APP_SECRET, raw_body))`.
- **Parse:** iterate `entry[].changes[].value.messages[]`. Types: `text.body`; `audio.id` (voice notes have `audio.voice=true`); `image.id` + `image.caption`; `document.id`; `location.latitude/longitude`; `reaction.message_id` + `reaction.emoji`; `interactive.button_reply.title`. Handle is `from` normalised to E.164. Group messages carry a group identifier in the payload; map it to `external_thread_id` and set `scope=group`. Confirm the exact group field name against the current Groups API webhook reference at build time. `value.statuses[]` updates `outbox` delivery state and is otherwise ignored.
- **Media:** `GET /{WA_API_VERSION}/{media_id}` with bearer token returns a short-lived `url`; `GET url` with the same bearer returns bytes.
- **Send:** `POST /{ver}/{WA_PHONE_NUMBER_ID}/messages` with `{messaging_product:"whatsapp", to, type:"text", text:{body}, context:{message_id}}`. Reaction: `type:"reaction", reaction:{message_id, emoji}`. Group sends use the Groups API recipient form for the group id. Template: `type:"template", template:{name, language:{code:"en_GB"}, components:[{type:"body", parameters:[{type:"text", text}]}]}`.
- **24-hour window:** `capabilities.proactive_window_hours = 24`. The router computes `last_inbound_at` per thread from `messages`; outside the window a text send becomes `send_template(WA_REMINDER_TEMPLATE, [text[:900]])`.
- **Group creation:** the dashboard's "Create WhatsApp group" button calls the Groups API, stores the thread, sets it as the household's primary thread, and shows the invite link and a QR code for both adults.

### 6.3 iMessage (BlueBubbles)

- **Setup:** an always-on Mac signed into a dedicated Apple ID, running BlueBubbles Server, reachable from the cluster over Tailscale. Configure a webhook for `new-message` to `{PUBLIC_BASE_URL}/webhooks/imessage?secret={BB_WEBHOOK_SECRET}`. Private API (needed for tapbacks and threaded replies) is optional and controlled by `BB_PRIVATE_API`.
- **Verify:** `secret` query param equals `BB_WEBHOOK_SECRET` (BlueBubbles does not sign payloads). Also reject anything where `data.isFromMe` is true.
- **Parse:** `data.guid` is the message id; `data.chats[0].guid` is the thread (`iMessage;+;chat...` means group, `iMessage;-;<handle>` means DM); `data.handle.address` is the handle; `data.text`; `data.attachments[]` with `guid`, `mimeType`, `transferName`. Audio messages arrive as `.caf` and are converted with `ffmpeg -i in.caf -ar 16000 out.wav` before transcription. Tapbacks arrive as messages with an `associatedMessageGuid` and type; map them to `reaction_emoji`.
- **Media:** `GET {BB}/api/v1/attachment/{guid}/download?password={BB_PASSWORD}`.
- **Send:** `POST {BB}/api/v1/message/text?password=...` with `{chatGuid, message, tempGuid: uuid4(), method: "private-api" | "apple-script"}`. Tapbacks via the react endpoint only when `BB_PRIVATE_API=true`, otherwise `NotSupported`.
- **Health:** the worker calls `GET /api/v1/ping` every 5 minutes; when it fails, the router marks the adapter degraded and falls back to each member's next identity, and DMs the admin member once per outage.
- Pin the BlueBubbles server version and verify these paths against its API docs; field names shift between releases.

## 7. Inbound pipeline and outbound router

Webhooks only persist; the worker does everything else. This keeps webhook latency under 100 ms, makes provider retries harmless, and serialises work per household so two messages never race on stock.

### 7.1 Webhook handler (api process)

1. Read raw body; `adapter.verify(request, body)`; 401 on failure.
2. `events = adapter.parse(body)`. A parse error is logged and answered 200 (never let a bad payload trigger a retry storm). A database outage returns 503 so the provider retries.
3. For each event: drop if `sender_handle` is a bot identity. Look up `channel_identities(channel, sender_handle)`.
   - Unknown sender whose text matches `^[A-Z]{4}-[A-Z0-9]{4}$`: redeem invite (section 12). Unknown sender otherwise: log, store nothing, reply nothing. Rate limit 5 invite attempts per handle per hour.
   - Known sender: upsert `threads(channel, external_thread_id)` under the member's household. If the thread is a group and `households.primary_thread_id` is null, set it.
4. `INSERT INTO messages (...) ON CONFLICT (thread_id, external_id) DO NOTHING`, status `received`, `meta` holding reply and reaction fields. Then `NOTIFY inbound, '<household_id>'`.
5. Return 200.

### 7.2 Processing (worker)

1. Wake on `NOTIFY inbound` or every 2 s. Select households that have `received` messages whose newest `created_at` is older than `DEBOUNCE_SECONDS`. This batches "out of eggs", "and bread", "oh and milk" into one turn.
2. In one transaction take `pg_advisory_xact_lock(hashtext(household_id))`, then `SELECT ... FOR UPDATE SKIP LOCKED` that household's `received` messages, oldest first, grouped by thread; set status `processing`.
3. For each media ref: `fetch_media`, store it through MediaStore (section 7.4), for S3 at `{household}/{message_id}/{n}.{ext}`, set `storage_key`. Audio: convert if needed, transcribe, set `transcript`. Images stay as references for the agent turn.
4. Build the `Envelope`. Text is assembled in order: each message's text, `[voice note] {transcript}`, `[photo] {caption}`, and reactions as `[reacted \u2705 to: "{first 120 chars of target message}"]`. In a group, each line is prefixed with the sender's name.
5. `result = await runtime.handle(envelope, ctx)` (section 8).
6. Response policy:
   - `result.noop`: send nothing.
   - `result.ack_only`: queue `OutboundMessage(target=thread, react_emoji="ack", reply_to_message_id=last message)`.
   - otherwise queue the reply text to the same thread, as a reply to the last message when the batch had more than one message.
7. Mark messages `processed` (or `failed` with the error in `meta`; failed turns send "Sorry, that didn't go through, try again?" once).

### 7.3 Outbound router (worker)

All sends go through `outbox`. Tools and jobs insert rows inside their own transaction, so a rolled-back turn sends nothing.

1. Poll every 2 s: `status='pending' AND send_after <= now()`, `FOR UPDATE SKIP LOCKED`, limit 20.
2. Resolve destinations:
   - `thread`: that thread's channel and external id.
   - `member`: the member's `preferred_channel` identity, DM thread via `adapter.dm_thread_id(handle)`; if that adapter is degraded, the next identity in order telegram, whatsapp, imessage.
   - `household`: `primary_thread_id` if set, else one row per adult member.
3. Quiet hours: if `respect_quiet_hours` and urgency is not `high` and the recipient (or, for a group, any adult) is inside quiet hours in household time, set `send_after` to the end of quiet hours and skip.
4. WhatsApp window: if `capabilities.proactive_window_hours` is set and the thread's last inbound message is older, send via template.
5. Text over `max_text_len` is split on paragraph boundaries. `react_emoji="ack"` resolves to `capabilities.ack_emoji`; `NotSupported` on react falls back to sending the emoji as text.
6. Success: `status='sent'`, store `external_id`, insert an `out` row into `messages`. Failure: `attempts += 1`, back off 10 s, 30 s, 2 min, 10 min, 30 min, then `failed` and try the member's next channel once.
7. `dedupe_key` is unique per household, so jobs can insert idempotently.

### 7.4 Media storage

`MediaStore` (`app/media/store.py`) has three methods: `put(household_id, message_id, n, data, mime) -> MediaRef`, `get(ref) -> bytes`, `delete(ref)`. The pipeline and the agent only ever see `MediaRef`, so the backend is one env var.

|  | S3 / MinIO (`s3`, default) | ImgBB (`imgbb`) |
| --- | --- | --- |
| Stores | Images, audio, documents | Images only |
| Privacy | Private bucket; nothing public | Anyone with the URL can view it (unlisted, not searchable) |
| Setup | A bucket and credentials | One API key |
| Retention | Bucket lifecycle rule + `media_cleanup` job | `expiration` on upload, 60 s to 180 days |
| Cost | Storage on your cluster or cloud | Free tier |

ImgBB behaviour: upload `POST https://api.imgbb.com/1/upload` with `key`, base64 `image` and `expiration = min(MEDIA_RETENTION_DAYS, 180) * 86400`; store `data.id`, `data.url` and `data.delete_url`. Audio and documents are never persisted with this backend: they are transcribed or read in memory and only the transcript is kept. For the agent turn, image bytes are fetched from `storage_url` and sent inline, so the LLM provider never needs the public URL.

Recommendation: start on `imgbb` if you want zero infrastructure, but use `s3` once real receipts flow through, since receipts can show your address and partial card details. Confirm ImgBB limits against its API page at build time.

## 8. Agent runtime

The agent is one interface with two implementations. v1 ships `LoopRuntime`; `LettaRuntime` is milestone 6 and must pass the same eval suite before it can be switched on.

```python
# app/agent/base.py
class ToolCallRecord(BaseModel):
    name: str
    args: dict
    result: str
    is_error: bool

class AgentResult(BaseModel):
    reply: str | None
    ack_only: bool = False      # model answered exactly ACK
    noop: bool = False          # model answered exactly NOOP
    tool_calls: list[ToolCallRecord] = []

class AgentRuntime(Protocol):
    async def handle(self, env: Envelope, ctx: Ctx) -> AgentResult: ...
```

### 8.1 Model-agnostic LLM layer

The agent talks to `LLMClient`, never to a vendor SDK. Neutral types live in `app/llm/types.py`; each adapter translates them to and from one wire format. Switching from one model to another is a config change, gated by the eval suite.

```python
# app/llm/types.py
class TextPart(BaseModel):
    type: Literal["text"] = "text"
    text: str

class ImagePart(BaseModel):
    type: Literal["image"] = "image"
    mime: str
    data_b64: str

class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict

class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "tool"]
    content: list[TextPart | ImagePart] = []
    tool_calls: list[ToolCall] = []        # assistant only
    tool_call_id: str | None = None        # tool only
    is_error: bool = False                 # tool only

class ToolDef(BaseModel):
    name: str
    description: str
    parameters: dict                       # JSON Schema from the Pydantic args model

class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0

class LLMResponse(BaseModel):
    text: str | None
    tool_calls: list[ToolCall] = []
    stop: Literal["end", "tool_calls", "length", "error"]
    usage: Usage = Usage()

class LLMClient(Protocol):
    supports_images: bool
    async def complete(self, system: str, messages: list[ChatMessage], tools: list[ToolDef],
                       max_tokens: int = 1024, temperature: float = 0.2) -> LLMResponse: ...

def make_llm(settings: Settings) -> LLMClient: ...   # switch on LLM_PROVIDER
```

| Neutral concept | `openai_compat` (Chat Completions) | `anthropic` (Messages) |
| --- | --- | --- |
| System prompt | First message with role `system` | Top-level `system`; static part marked cacheable |
| `ToolDef` | `tools[{type: "function", function: {name, description, parameters}}]` | `tools[{name, description, input_schema}]` |
| Model asks for tools | `message.tool_calls[].function.arguments` (JSON string, parsed) | `tool_use` content blocks (`input` object) |
| Tool result | Message with role `tool` and `tool_call_id` | User message with `tool_result` block, `is_error` |
| `ImagePart` | `image_url` with a `data:` URI | `image` block with base64 source |
| Stop reason | `finish_reason`: `tool_calls`, `stop`, `length` | `stop_reason`: `tool_use`, `end_turn`, `max_tokens` |

Adapter rules: malformed tool-argument JSON becomes a tool error ("invalid JSON arguments") so the model retries; providers that return one tool call at a time work unchanged because the loop just iterates; prompt caching is an adapter optimisation, never a requirement; retries with backoff on 429 and 5xx live in the adapter.

Why not LiteLLM: it covers far more providers through one call, but it is a large, fast-moving dependency. Two small adapters cover almost every provider, because most expose an OpenAI-compatible endpoint. A `litellm` adapter can still be added later behind the same protocol.

### 8.2 LoopRuntime

1. Build `system` = static prompt + dynamic household brief.
2. Build `messages` (neutral `ChatMessage`) = thread history + current turn. History: last 20 messages of this thread from the past 48 h, member messages as `user` ("{name}: {text}"), agent messages as `assistant`. Current turn: `env.text` plus up to 4 `ImagePart`s loaded through `MediaStore`.
3. If images are present but `llm.supports_images` is false, drop them and append `[photo received; this model can't read photos]` to the turn; the prompt tells the agent to say so briefly.
4. `tools = tool_definitions(onboarding_active)` from the registry.
5. Loop up to `LLM_MAX_TOOL_ITERATIONS`: `resp = await llm.complete(system, messages, tools)`. If `resp.tool_calls`: append the assistant message, run each call in order through `run_tool(name, args, ctx)`, append one `tool` message per call (`is_error=true` for validation or domain errors, with the message, so the model can recover), and continue. Otherwise stop.
6. Final text: strip; `ACK` sets `ack_only`, `NOOP` sets `noop`; anything else is the reply. Hitting the iteration cap sends "I got a bit lost there, can you say that again?"
7. `run_tool` validates args with Pydantic and calls the matching function in `app/services/` inside its own transaction, which also writes `agent_actions` (args, result, inverse). A later failure does not undo earlier successful writes in the same turn.
8. Token usage per turn is stored in `messages.meta.usage` so cost per provider is visible on the dashboard.

**Household brief** (`prompt.py`, capped at about 1,500 tokens; truncate lists with "+N more"):

```text
Now: {weekday} {date} {HH:MM} ({timezone})
Speaking: {member_name} ({channel}, {dm|group})
Family: Ola (adult), ... , Tobi (child), ...
Locations: fridge, freezer, store, ...
Facts: milk_brand=Cravendale; main_supermarket=Tesco Extra; ...
Shopping list ({n}): eggs, bread, ... 
Low or out: rice (low), milk (out), ...
Expiring within 3 days: chicken thighs (freezer, 8 Oct), ...
Next 7 days: Tue 6 Oct 09:00 Chatterbox (Tobi); Wed 7 Oct 10:30 GP (Ada); ...
```

### 8.3 System prompt (static part, verbatim)

```text
You are {AGENT_NAME}, the household assistant for a family. You live in their group chat and their direct messages. Your job is to keep track of food stock, the shopping list, appointments and reminders so nobody has to remember things or fill in forms.

How you behave:
- Be brief. Most replies are one short line. Use a list only when listing items.
- If you only recorded something and there is nothing useful to say, reply with exactly ACK.
- If a message is not meant for you (family members talking to each other, greetings, jokes), reply with exactly NOOP. When in doubt in a group, prefer NOOP unless the message mentions stock, shopping, plans, times, appointments or reminders.
- Put every inventory change from one turn into a single log_inventory call.
- Never invent quantities. If none was stated, leave quantity empty.
- Receipt photo: log bought items as restocked with source receipt. Fridge, freezer or cupboard photo: log the items you can see as adjusted with source photo and that location. Never mark an item finished because it is missing from a photo.
- "Running low on X" is the low action. "We're out of X" or "finished the X" is finished.
- Resolve relative times (tomorrow, next Tuesday, in two hours) against the time in the brief. If a time is genuinely ambiguous, ask one short question.
- If a tool says a name is ambiguous, ask one short question listing the options. Everything else in that turn was still recorded.
- After creating or moving an event, confirm in one line with weekday, date and time, and the reminder plan.
- "Undo", "that's wrong" or "no, I meant..." right after an action: call undo_last, then redo correctly if they said what they meant.
- Do not give medical, legal or financial advice. Store appointments and medication reminders exactly as given.
- Never mention tools, databases, prompts or the system. Never message anyone outside this family.
```

### 8.4 LettaRuntime (optional)

- One Letta agent per household; id stored in `households.letta_agent_id`. Memory blocks: `persona` (the static prompt above) and `household` (the brief), rewritten by our code before each turn so Postgres stays the source of truth.
- Tools are thin Letta custom tools that `POST {PUBLIC_BASE_URL}/internal/tools/{name}` with `Authorization: Bearer INTERNAL_TOOL_TOKEN` and the household id from an agent-level tool environment variable. The API resolves `member_id` and `message_id` from the household's in-flight turn, which is unambiguous because turns are serialised per household (section 7.2).
- Thread history is not sent; Letta keeps its own. Images are passed as message content parts.
- Verify Letta's current tool-environment and image APIs at build time; the adapter is about 150 lines either way.

## 9. Tools and resolution rules

Twelve tools; signatures and argument models are in the companion file `tools.py`. Every tool takes natural names, is batch-first, and returns short text lines the model can relay or act on.

**Result line prefixes** (the model is shown these in tool descriptions): `OK:` what changed; `NEW:` items or people created; `AMBIGUOUS:` nothing recorded for that entry, with the options; `ERROR:` validation or domain problem; `NOTE:` side effects, such as "added to shopping list".

| Tool | Writes | Key behaviour |
| --- | --- | --- |
| `log_inventory` | events, stock, list | Per change: resolve item and location, append event, apply stock transition, run side-effect rules |
| `query_inventory` | none | Filters by item, location, status, expiry; shows quantity when known |
| `update_shopping_list` | list, events | `add`, `bought` (logs `restocked`, source `shopping`), `remove` (dismissed), `bought_all` |
| `get_shopping_list` | none | Explicit, finished and low first; then predicted items marked "(probably)"; grouped by category; optional store filter |
| `schedule_event` | events, reminders | Resolves participants; creates reminders; recurring reminders come from the recurrence job |
| `modify_event` | events, reminders | Move, edit or cancel; scope `this` on a recurring event adds an exception date (and a one-off event when moved) |
| `list_upcoming` | none | Expands RRULEs in window, includes standalone reminders |
| `set_reminder` | reminders | One-off (`fire_at`) or repeating (`rrule`); target me, household or a name |
| `remember` | facts | Upsert; `value=None` deletes |
| `add_family_member` | members | Adds a child or adult record (no channel); adults get invited via CLI |
| `undo_last` | inverse of last action | Within 24 h, by the same member; `n` up to 5 |
| `onboarding_advance` | households | Only exposed during onboarding (section 12) |

### 9.1 Item resolution (`resolve.py`)

1. Normalise: lowercase, trim, strip leading articles and quantities ("a dozen", "2x"), singularise the last word with `inflect` ("eggs" to "egg"; keep a short exception list such as "oats", "noodles", "peas").
2. Exact match on `canonical_name` or any alias: hit.
3. Trigram similarity (`similarity()` from pg\_trgm) against names and aliases, threshold 0.55. One candidate, or the best leads the next by 0.15 or more: hit, and the raw input is appended to `aliases`.
4. Several close candidates: `AMBIGUOUS: 'pepper' could be Scotch bonnet (freezer), Black pepper (store), Bell pepper (fridge)`.
5. No candidate: create the item. `canonical_name` = normalised input in its original casing; `default_location` = given location or `store`; category left null for the nightly job to fill. Report `NEW: Scotch bonnet (freezer)`.

Location resolution uses the same steps against `locations` (aliases: "deep freezer", "pantry" and "cupboard" map to existing rows by trigram). Member resolution: "me" and "I" mean `ctx.member_id`; "us" means all adults; "the kids" means all children; otherwise trigram on names.

### 9.2 Calendar rules

- Times from the model arrive as ISO 8601 with offset; naive times are interpreted in household time zone.
- `remind_before_minutes` default `[1440, 60]`. The 1,440-minute reminder is skipped if it would land in quiet hours; it moves to the morning digest instead.
- Reminder target: `household` if participants include a child or more than one adult, else that adult as `member`.
- Reminder text format: `{title} {relative day} at {HH:MM}{, location}` (e.g. "GP for Ada tomorrow at 10:30, Hurley Clinic").
- Non-recurring events get reminder rows immediately. Recurring events get rows from the hourly recurrence job for occurrences in the next 48 h, expanded with `dateutil.rrule` using a time-zone-aware `dtstart` in household time so DST is handled (UK clocks change on 25 Oct 2026).
- Event matching for `modify_event`: active events whose next occurrence is within 60 days, ranked by trigram similarity on title and participant names, tie broken by soonest.

### 9.3 Undo machinery (`actions.py`)

Each tool returns its writes plus an inverse, stored in `agent_actions.inverse` as a list of ops:

```json
[
  {"op": "restore_rows", "table": "stock", "rows": [{"item_id": "...", "location_id": "...", "qty_estimate": 6, "status": "in_stock", "last_event_at": "..."}]},
  {"op": "delete_rows", "table": "shopping_list_items", "ids": ["..."]},
  {"op": "restore_rows", "table": "events", "rows": [{"id": "...", "starts_at": "..."}]},
  {"op": "delete_rows", "table": "reminders", "ids": ["..."]}
]
```

`restore_rows` upserts the full prior row (rows that did not exist before become `delete_rows`). `inventory_events` is never deleted: undo appends an `adjusted` event with source `undo` per affected stock row so the log still replays correctly. Undo applies the newest non-undone action first and sets `undone_at`. An undo is refused with `ERROR:` if a later action by anyone touched the same rows.

### 9.4 Deterministic side-effect rules

- `finished` or `discarded` to zero on a staple: add to list, reason `finished`, `NOTE:` in the result.
- `low` on any item: add to list, reason `low`.
- `restocked` or `added`: any active list entry for that item becomes `bought`.
- Auto-staple: after an item completes two restock-to-finished cycles, set `is_staple=true`.
- `update_shopping_list.bought` with no quantity logs `restocked` with null quantity and the item's default location.

## 10. Scheduler and background jobs

The worker is a single asyncio supervisor (`worker/main.py`) that runs each job as its own task with its own interval, restarts a crashed task after 5 s, and logs a heartbeat every minute. Daily jobs run per household in household time and claim `job_runs(job, household_id, run_key)` with `ON CONFLICT DO NOTHING`, so a restart or a second replica never doubles a digest.

| Job | Cadence | What it does |
| --- | --- | --- |
| `inbound` | NOTIFY + 2 s poll | Section 7.2 |
| `outbox` | 2 s | Section 7.3 |
| `reminders` | 15 s | Due `scheduled` reminders go to `outbox` (dedupe key `reminder:{id}:{fire_at}`). One-off: status `sent`. RRULE: compute next occurrence, update `fire_at`, stay `scheduled` |
| `recurrence` | hourly | For active recurring events, insert reminder rows for occurrences in the next 48 h, skipping `exdates`; unique `(event_id, fire_at)` makes it idempotent |
| `daily_brief` | daily at `digest_time` (07:30) | Only if something is due today: today's events, reminders, items expiring within 2 days. One message to the household thread |
| `weekly_digest` | Sunday 18:00 | The week ahead, shopping list count, predicted lows, expiring items |
| `consumption_model` | daily 03:00 | Recompute `consumption_profiles` (below); fill null `items.category` in one batched LLM call per household (fast model, JSON output) |
| `low_stock_prompt` | daily 17:30 | Items predicted to run out within 2 days, not on the list, not prompted in 3 days: "Probably running low: milk, bread. Add to the list?" A "yes" or \\u2705 reaction is handled by the agent from thread context |
| `imessage_health` | 5 min | BlueBubbles ping; adapter degraded flag |
| `media_cleanup` | daily 04:00 | Delete S3 objects older than `MEDIA_RETENTION_DAYS`; null `storage_key`; transcripts remain |

**Consumption model.** For each item, build cycles from `inventory_events`: a cycle starts at `restocked` or `added` and ends at the next `finished`, `low` or `discarded`. Ignore cycles shorter than 12 hours or longer than 120 days. `avg_days_to_finish` is an exponentially weighted mean of cycle lengths (alpha 0.5, newest weighted most). Require `samples >= 2`. `predicted_runout_at = last_restocked_at + avg_days_to_finish`. This needs no ML library and is good enough to drive gentle prompts.

**Quiet hours.** Defaults 21:30 to 07:00 per member; windows that cross midnight are handled by comparing local times as `start > end`. `urgency=high` (used for store-arrival nudges and reminders the user explicitly set inside quiet hours) bypasses them.

## 11. Presence and store nudges

Geofences live on each phone as iOS Personal Automations; the server only receives "Ola entered Tesco Extra". No app, no background location, no coordinates stored unless you want them.

**Endpoint.** `POST /presence/{token}` with JSON `{"event": "enter" | "exit", "place": "Tesco Extra"}`. The token is per adult (32 random bytes, URL-safe), stored as SHA-256 in `members.presence_token_hash`, shown and rotatable on the dashboard Settings page. Unknown place names create a `places` row of kind `other`, which can be re-typed on the dashboard. Response: `204` always (even for unknown tokens, to avoid probing), rate limit 30 per token per hour.

**Phone setup** (once per place, about a minute each): Shortcuts, Automation, New, "Arrive" (or "Leave" for Home), choose the location, set "Run Immediately", add the action "Get Contents of URL" with method POST, JSON body `event` and `place`. Onboarding DMs each adult their personal URL and these steps.

**Rules** (`presence/rules.py`, pure code, no LLM):

| Rule | Trigger | Condition | Action |
| --- | --- | --- | --- |
| Store arrival | `enter` a place of kind `store` | Active list has 1+ non-predicted items; no `nudge_log` key `store:{member}:{place}` in the last 2 h | DM that member the list filtered to this store (entries with matching `store_hint` or no hint), urgency `high`, plus a trailing "(probably)" section |
| Out and about | `exit` Home | List has 8+ explicit items or an entry added in the last 2 h; once per member per day | DM: "You're out. The list has 9 items, want it?" A reply triggers `get_shopping_list` through the agent |
| Both home | `enter` Home | All adults now home and a store nudge was sent today | Nothing sent; marks the day's nudges resolved (prevents a late "out and about") |

Presence is optional by design: if nobody sets up Shortcuts, everything else still works.

## 12. Onboarding

Setup is a two-minute web page for the admin, then a conversation. Nobody fills in a form, and any step can be skipped with "skip" and done later just by talking.

### 12.1 Bootstrap (web, once)

1. Deploy with `SETUP_TOKEN` set and open `{PUBLIC_BASE_URL}/setup?token=...`. While no household exists, this page asks for household name, time zone and the admin's name. Submitting creates the household and the admin member, seeds the `fridge`, `freezer` and `store` locations, and shows the admin's invite. `/setup` then returns 404 forever.
2. The admin taps their invite. Once connected, "dashboard" sent to the bot returns a login link (section 13).
3. On the Family page, the admin adds the second adult and shares their invite (link, QR code or code).
4. Optional, on the Channels page: "Create WhatsApp group", or add the bot or Apple ID to an existing family group.

Invite codes are 4 letters, hyphen, 4 alphanumerics (no 0/O/1/I), valid 7 days, stored hashed. Each invite shows three ways in: a Telegram deep link (`https://t.me/{TG_BOT_USERNAME}?start={code}`), a QR code of that link, and the raw code to send to the bot on WhatsApp or iMessage. Redemption creates the `channel_identities` row and replies "Hi {name}, you're connected." The same code works once per channel, so one invite links all of a person's channels. The first redeemed channel becomes `preferred_channel`.

Groups: add the bot (or the Apple ID, or join the WhatsApp invite) to a family group. The first message there from a known member creates the thread and sets it as `primary_thread_id`.

### 12.2 Conversational setup

When the first adult connects, `onboarding_state` is `{"step": "family", "done": []}` and the agent gets an extra prompt section plus the `onboarding_advance` tool. The agent asks one thing at a time and calls `onboarding_advance(step)` when a step is done or skipped.

| Step | Agent asks | Writes |
| --- | --- | --- |
| `family` | Who lives here, including the kids? | `add_family_member` for each child |
| `routines` | Regular things: nursery, classes, clubs, with days and times? | Recurring `events` (e.g. Chatterbox weekly) |
| `shops` | Where do you usually shop? Any specialist shops? | `places` kind `store`; fact `main_supermarket` |
| `staples` | What do you always need to keep in the house? | Items with `is_staple=true` |
| `tour` | Send photos of the fridge, the freezer and the store cupboard | `log_inventory` adjusted, source `photo` |
| `rhythm` | Morning brief at 07:30 and quiet from 21:30 to 07:00, OK? | `digest_time`, quiet hours |
| `presence` | DMs each adult their Shortcut URL and steps | none |

Onboarding prompt section, appended to the system prompt while active:

```text
ONBOARDING. You are setting up this household. Current step: {step}. Remaining: {remaining}.
Ask one short, friendly question for the current step. Accept partial answers. When the step is complete or the user says skip, call onboarding_advance with the step name. Do not ask about steps already done. When nothing remains, say setup is done and that they can just talk to you normally from now on.
```

The second adult does not repeat steps already done; they get a one-line welcome and the `presence` DM only.

## 13. Web dashboard

A phone-first dashboard at `/dashboard`, rendered by the api process with Jinja2 and HTMX. It is for setup, looking things over and fixing mistakes; daily use still happens in chat, so the dashboard never needs to be opened for the system to work.

**Login by magic link over chat, no passwords.** A member sends "dashboard" to the bot. The pipeline intercepts this exact keyword before the agent (no LLM call) and DMs a one-time link `/login/{token}`: 32 random bytes, hashed in `login_tokens`, 10-minute expiry, single use, 5 per member per hour. Opening it shows a page with one button; the button's POST spends the token (a GET never does, ADR 0035) and sets a signed, HttpOnly, Secure, SameSite=Lax cookie for 30 days carrying the member id and `members.session_version`; "Log out everywhere" increments that version. Children never get logins. All adults can view and edit; only `is_admin` members see the System page.

| Page | Shows | Actions |
| --- | --- | --- |
| Today | Today's and tomorrow's events, due reminders, list count, low and expiring items | Quick-add to the list |
| Shopping list | Active list by category, then a "probably needed" section | Add, tick off, remove, set store hint |
| Inventory | Stock by location, filter by status; per-item event history | Adjust, mark finished, edit item (aliases, staple, threshold, default location), merge duplicates |
| Calendar | Next 30 days, recurring series, standalone reminders | Add, edit, cancel, skip one occurrence; copy ICS subscribe link |
| Family | Members, connected channels per person, open invites | Add adult or child, create or revoke invite (link, QR, code), preferred channel, quiet hours |
| Channels | Adapter health, threads, primary group | Create WhatsApp group, set primary thread |
| Activity | Last 200 turns: messages, tool calls and results, tokens, latency, outbox status | Undo an action, retry a failed send |
| Playground | Chat with the agent in the browser | Dry run by default (whole turn in a rolled-back transaction); toggle to apply for real |
| Settings | Digest time, facts, each adult's presence URL with Shortcut steps, ICS link | Rotate presence and ICS tokens, edit facts |
| System (admin) | Job runs, worker heartbeat, LLM provider and model, last eval result, version | Export household JSON, rebuild stock |

**Implementation rules**

- Every write goes through `app/services/`, so stock rules, undo and outbox behave exactly as they do from chat. Dashboard writes are logged in `agent_actions` with `source = 'dashboard'` and appear in Activity.
- Each table or list is an HTMX partial (`hx-get` to refresh, `hx-post` to change); no client-side state, no build step. One hand-written CSS file, system fonts, dark mode via `prefers-color-scheme`.
- CSRF: a token derived from `SESSION_SECRET` and the session, sent on every POST via `hx-headers`; mismatches return 403.
- Merging duplicate items reassigns events, stock, list entries and aliases to the kept item in one transaction.
- The dashboard is on the public ingress by default so it works from phones anywhere; set `DASHBOARD_PUBLIC=false` to serve it only on the tailnet.

## 14. HTTP API reference

Everything except `/internal` is on the public ingress (the dashboard routes only when `DASHBOARD_PUBLIC=true`). `/internal` is reachable only inside the cluster.

| Method | Path | Auth | Response | Purpose |
| --- | --- | --- | --- | --- |
| GET | `/healthz` | none | 200 | Liveness |
| GET | `/readyz` | none | 200 / 503 | DB reachable, migrations current |
| POST | `/webhooks/telegram` | secret header | 200 | Inbound Telegram |
| GET | `/webhooks/whatsapp` | verify token | challenge text | Meta subscription check |
| POST | `/webhooks/whatsapp` | HMAC signature | 200 | Inbound WhatsApp + statuses |
| POST | `/webhooks/imessage` | `secret` query | 200 | Inbound BlueBubbles |
| POST | `/presence/{token}` | token | 204 | Shortcut enter/exit |
| GET | `/ics/{token}.ics` | token | `text/calendar` | Read-only event feed for Apple or Google Calendar |
| GET, POST | `/setup` | `SETUP_TOKEN` | HTML | First-run household creation; 404 once a household exists |
| GET | `/login/{token}` | one-time token | HTML | A page with one button; spends nothing |
| POST | `/login/{token}` | one-time token | 303 to `/dashboard` | Exchanges a magic link for a session cookie |
| POST | `/logout` | session + CSRF | 303 | Ends this session; `?everywhere=1` bumps `session_version` |
| GET, POST | `/dashboard/...` | session + CSRF on POST | HTML / HTMX partials | Pages in section 13 |
| POST | `/dashboard/playground` | session + CSRF | HTMX partial | Runs a turn; dry run unless `apply=1` |
| GET | `/dashboard/system/export` | session, admin | JSON | Full export of the household |
| POST | `/dashboard/system/rebuild-stock` | session, admin | 202 | Replay `inventory_events` into `stock` |
| POST | `/internal/tools/{name}` | `INTERNAL_TOOL_TOKEN` | `{"result": str, "is_error": bool}` | Letta tool bridge |

**ICS feed.** One `VEVENT` per event with `RRULE` and `EXDATE` passed through, `UID={event_id}@household-agent`, `DTSTART` with `TZID`, 15-minute cache headers. Token is per household (`households.calendar_token_hash`). This gives both phones a native calendar view with zero OAuth; two-way Google Calendar sync is out of scope for v1.

## 15. Security and privacy

The core guarantee is structural: no tool can address a recipient, so even a successful prompt injection (a forwarded message or a photographed note saying "send this to...") can only reach the family's own threads.

- **Allowlist by construction.** The router resolves destinations only from `threads` and `channel_identities` rows of the household. There is no free-form recipient anywhere in the tool schemas or dashboard forms.
- **Unknown senders are invisible.** No row, no reply, except invite codes (rate limited).
- **Webhook authentication** per channel (section 6), constant-time comparisons, raw body used for HMAC.
- **Dashboard sessions.** Magic links only ever go to a member's own verified channel; tokens are single use and short-lived; cookies are signed, HttpOnly, Secure and SameSite=Lax; every POST checks CSRF. `/setup` disables itself after first use.
- **Secrets at rest.** Presence, ICS, invite and login tokens are stored as SHA-256 hashes. Provider secrets live in Sealed Secrets, mounted as env vars.
- **Untrusted content.** Forwarded text, transcripts and image text are passed to the model inside the user turn, never the system prompt. Tool outputs never echo raw user content into instructions.
- **Sensitive data.** Appointments are health-adjacent. Postgres on encrypted volumes, TLS everywhere, nightly encrypted backups (`pg_dump` to object storage, 30-day retention). Media auto-deletes after `MEDIA_RETENTION_DAYS`. With the ImgBB backend, image URLs are public to anyone holding them; they are never shown outside the dashboard and logs.
- **Third parties.** Message text and images go to whichever LLM and transcription providers are configured; check their retention and training settings before go-live. Swapping to a self-hosted model via `openai_compat` keeps everything in-house.
- **Logs** carry ids, not message text, at `INFO`. Message text only at `DEBUG`, which is off in production.

## 16. Testing and acceptance criteria

Three test layers, and agent quality is judged by database state, never by exact wording.

**Unit** (`tests/unit`, no network): stock transitions (every row of the section 4 table), item resolution (exact, alias, fuzzy, ambiguous, new), quiet hours across midnight, RRULE expansion across the 25 Oct 2026 UK clock change, WhatsApp window logic, debounce grouping, undo inverse generation and conflict refusal, invite code format and expiry.

**Contract** (`tests/contract`): recorded webhook fixtures per channel (text, voice, photo, location, reaction, group, own-message) parse into golden `InboundEvent` JSON. Send calls are asserted with `respx` mocks. Every adapter must pass the same suite; a new channel is done when its fixtures pass.

**Agent evals** (`tests/evals/*.yaml`, run nightly against real models with Postgres in testcontainers): each case seeds state, sends one or more messages through the same `simulate_turn()` service the dashboard Playground uses, and asserts on rows. Example cases:

```yaml
- name: batch finish with staple
  seed: {items: [{name: egg, staple: true, stock: {fridge: 6}}, {name: bread}]}
  turns: [{from: Ola, text: "we're out of eggs and bread"}]
  expect:
    events: [{item: egg, type: finished}, {item: bread, type: finished}]
    shopping_list_active: [egg]
    reply: ACK

- name: chit-chat is ignored
  turns: [{from: Wife, scope: group, text: "love you, see you at 6"}]
  expect: {writes: 0, reply: NOOP}

- name: appointment then move
  turns:
    - {from: Ola, text: "Ada has GP on Wednesday at 10:30 at Hurley Clinic"}
    - {from: Ola, text: "actually make it 11"}
  expect:
    events: [{title_contains: GP, participants: [Ada], local_start: "Wed 11:00"}]
    reminders_scheduled: 2

- name: undo
  turns: [{from: Ola, text: "finished the rice"}, {from: Ola, text: "undo"}]
  expect: {stock: {rice: {status: in_stock}}, shopping_list_active: []}
```

Target suite size: 40 cases covering inventory, list, calendar, reminders, photos (3 receipt fixtures, 2 fridge fixtures), ambiguity, NOOP and undo. Pass bar for release: 95%, and 100% on the NOOP and undo cases. The suite runs on at least two providers (for example one model through openai\_compat and one through anthropic), so any accidental lock-in shows up as a failing test. A model swap in production requires a green run on that model first.

**v1 acceptance checklist**

- [ ] A group voice note with three inventory changes yields three events and one ack reaction within 10 s (p95).
- [ ] Replaying any webhook twice creates no duplicate rows or sends.
- [ ] Reminders fire within 60 s of `fire_at` and hold during quiet hours unless urgent.
- [ ] A store-arrival Shortcut call produces the filtered list within 30 s, at most once per 2 h per store.
- [ ] Changing a member's `preferred_channel` row is the only step needed to move their DMs.
- [ ] Telegram, WhatsApp and iMessage adapters pass the same contract suite, and the eval suite passes on two LLM providers.
- [ ] "Undo" restores stock and list exactly.
- [ ] Nothing is ever sent to a handle outside `channel_identities` (property test over the router).
- [ ] Stock rebuilt from `inventory_events` equals live `stock`.

## 17. Deployment and build milestones

Ship on your existing k3s stack: one Helm chart, one image, two Deployments, managed by ArgoCD, secrets via Sealed Secrets, CI in GitHub Actions.

**Runtime layout**

- `household-agent-api`: **1 replica**, `uvicorn app.main:app`. Rolling updates with `maxSurge: 1, maxUnavailable: 0` give zero-downtime deploys even at one replica, and every channel retries failed webhooks, so a restart loses nothing. The process is stateless, so scaling is a replica count or an HPA (CPU 70%, min 1, max 3) with no code change.
- `household-agent-worker`: 1 replica, `python -m app.worker.main`, liveness via a heartbeat file touched every minute. Also replica-safe (`SKIP LOCKED`, per-household advisory locks, `job_runs`), so it can scale out if this ever serves many households.
- Postgres 16 via CloudNativePG (or a managed Postgres), daily backups. MinIO only if `MEDIA_BACKEND=s3` and no bucket exists.
- Ingress with cert-manager TLS; path allowlist: `/webhooks/`, `/presence/`, `/ics/`, `/setup`, `/login/`, `/logout`, `/dashboard/`, `/healthz`.
- The BlueBubbles Mac joins the tailnet; the cluster reaches `BB_BASE_URL` over Tailscale and nothing on the Mac is public.
- CI: lint (ruff), type check (mypy on `app/core`, `app/llm`, `app/agent`), unit and contract tests on every push; evals nightly and on release tags against two providers; image pushed, ArgoCD syncs the tag.

**Milestones** (each ends with something the two of you use daily)

1. **Telegram, inventory and list.** Schema, `app/llm` with both adapters, Telegram adapter, inbound pipeline, `LoopRuntime`, inventory and shopping tools, undo, `/setup`, magic-link login, dashboard pages Today, Shopping list, Inventory, Activity and Playground. Exit: the market board comes down.
2. **Calendar and reminders.** Calendar tools, worker jobs `reminders`, `recurrence`, `daily_brief`, `weekly_digest`, ICS feed, Calendar page. Exit: Chatterbox and the next GP appointment remind you correctly across the 25 Oct clock change.
3. **Onboarding and photos.** Conversational onboarding, `MediaStore` (ImgBB or S3), receipt and fridge photos, Family and Settings pages. Exit: the second adult is set up without your help.
4. **WhatsApp.** Adapter, template approval, window logic, Channels page with group creation. Exit: contract suite green, a week of daily use.
5. **Presence and predictions.** Shortcut endpoint, rules, consumption model, `low_stock_prompt`. Exit: the list arrives at the shop without anyone asking.
6. **iMessage and Letta (optional).** BlueBubbles adapter with health fallback; `LettaRuntime` behind the flag, promoted only if it beats the loop on the eval suite; System page.

Each milestone also updates `docs/` (section 18) and adds an ADR for any decision it makes.

Schedule is set by your capacity; milestones 1 and 2 alone replace the market board and the forgotten appointments.

## 18. Documentation (`docs/`)

Docs live in the repo as plain Markdown that renders on GitHub, and a PR that changes behaviour updates the matching doc in the same PR (enforced by a PR template checkbox, not by CI). This spec, exported to Markdown, is committed as `docs/spec.md` and stays the v1 baseline.

| File | Contents |
| --- | --- |
| `README.md` | What it is, a 5-minute local start (`docker-compose.yml` at the repo root runs Postgres, MinIO, api and worker), links to everything below |
| `architecture.md` | Sections 1, 2, 5 and 7 condensed; the architecture diagram as Mermaid from `diagrams/architecture.mmd` |
| `data-model.md` | Table reference, stock transition table, invariants, an ERD generated from `schema.sql` |
| `channels.md` | Per-channel setup checklists (BotFather, Meta app and template, BlueBubbles), payload notes, known limits |
| `llm.md` | Provider layer, how to add an adapter, tested models with their latest eval scores |
| `agent-and-tools.md` | Prompt (source of truth stays `app/agent/prompt.py`), tool catalogue with examples, resolution rules, undo |
| `dashboard.md` | Pages, login flow, screenshots |
| `operations.md` | Deploy, env vars, backup and restore drill, token rotation, BlueBubbles outage, WhatsApp template rejection, cost tracking |
| `evals.md` | How to run, how to add a case, latest results per provider |
| `adr/NNNN-title.md` | One decision per file: Context, Decision, Consequences |

Seed ADRs, written in milestone 1:

1. `0001` Postgres is the source of truth.
2. `0002` Own model-agnostic LLM layer instead of LiteLLM.
3. `0003` Telegram first.
4. `0004` One api replica, scale by configuration.
5. `0005` Server-rendered HTMX dashboard instead of a SPA.
6. `0006` `MediaStore` with S3 and ImgBB backends.
7. `0007` iMessage via BlueBubbles.
