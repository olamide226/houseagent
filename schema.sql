-- Household agent schema v1.2 (Postgres 15+)
-- Applied verbatim as Alembic migration 0001.
-- Principle: Postgres is the source of truth. The agent only touches data through tools.

create extension if not exists pgcrypto;
create extension if not exists pg_trgm;

-- =========================================================
-- Household, members, channels, messages
-- =========================================================
create table households (
  id                  uuid primary key default gen_random_uuid(),
  name                text not null,
  timezone            text not null default 'Europe/London',
  digest_time         time not null default '07:30',
  onboarding_state    jsonb not null default '{"step": "family", "done": []}',
  calendar_token_hash text,                 -- sha256 of ICS feed token
  letta_agent_id      text,                 -- only when AGENT_RUNTIME=letta
  created_at          timestamptz not null default now()
);

create table members (
  id                  uuid primary key default gen_random_uuid(),
  household_id        uuid not null references households(id) on delete cascade,
  name                text not null,
  role                text not null check (role in ('adult', 'child')),
  is_admin            boolean not null default false,
  preferred_channel   text check (preferred_channel in ('telegram', 'whatsapp', 'imessage')),
  quiet_start         time default '21:30',
  quiet_end           time default '07:00',
  presence_token_hash text unique,
  invite_code_hash    text unique,
  invite_expires_at   timestamptz,
  session_version     int not null default 1,  -- bump to log out everywhere
  created_at          timestamptz not null default now()
);

-- Dashboard magic links, sent only to a member's own verified channel.
create table login_tokens (
  token_hash  text primary key,           -- sha256 of 32 random bytes
  member_id   uuid not null references members(id) on delete cascade,
  expires_at  timestamptz not null,       -- now() + 10 minutes
  used_at     timestamptz,
  created_at  timestamptz not null default now()
);
create index login_tokens_member_idx on login_tokens (member_id, created_at desc);

create table channel_identities (
  id          uuid primary key default gen_random_uuid(),
  member_id   uuid not null references members(id) on delete cascade,
  channel     text not null check (channel in ('telegram', 'whatsapp', 'imessage')),
  handle      text not null,              -- E.164 phone, Telegram user id, Apple ID
  verified_at timestamptz not null default now(),
  unique (channel, handle),
  unique (member_id, channel)
);

create table threads (
  id                 uuid primary key default gen_random_uuid(),
  household_id       uuid not null references households(id) on delete cascade,
  channel            text not null,
  external_thread_id text not null,
  scope              text not null check (scope in ('dm', 'group')),
  created_at         timestamptz not null default now(),
  unique (channel, external_thread_id)
);

alter table households add column primary_thread_id uuid references threads(id) on delete set null;

create table messages (
  id           uuid primary key default gen_random_uuid(),
  household_id uuid not null references households(id) on delete cascade,
  thread_id    uuid references threads(id),
  member_id    uuid references members(id),          -- null when the agent sent it
  direction    text not null check (direction in ('in', 'out')),
  text         text,
  media        jsonb not null default '[]',           -- list of MediaRef
  meta         jsonb not null default '{}',           -- reply_to, reaction, errors
  external_id  text,
  status       text not null default 'received'
               check (status in ('received', 'processing', 'processed', 'failed', 'sent')),
  created_at   timestamptz not null default now(),
  processed_at timestamptz,
  unique (thread_id, external_id)
);
create index messages_pending_idx on messages (household_id, created_at) where status = 'received';
create index messages_thread_recent_idx on messages (thread_id, created_at desc);

create table household_facts (
  id           uuid primary key default gen_random_uuid(),
  household_id uuid not null references households(id) on delete cascade,
  member_id    uuid references members(id),
  key          text not null,
  value        text not null,
  updated_at   timestamptz not null default now(),
  unique nulls not distinct (household_id, member_id, key)
);

-- =========================================================
-- Inventory: append-only log + projection
-- =========================================================
create table locations (
  id           uuid primary key default gen_random_uuid(),
  household_id uuid not null references households(id) on delete cascade,
  name         text not null,
  aliases      text[] not null default '{}',
  unique (household_id, name)
);

create table items (
  id                  uuid primary key default gen_random_uuid(),
  household_id        uuid not null references households(id) on delete cascade,
  canonical_name      text not null,
  aliases             text[] not null default '{}',
  category            text,
  default_unit        text,
  default_location_id uuid references locations(id),
  is_staple           boolean not null default false,
  low_threshold       numeric,
  created_at          timestamptz not null default now(),
  unique (household_id, canonical_name)
);
create index items_aliases_idx on items using gin (aliases);
create index items_name_trgm_idx on items using gin (canonical_name gin_trgm_ops);

create table inventory_events (
  id                uuid primary key default gen_random_uuid(),
  household_id      uuid not null references households(id) on delete cascade,
  item_id           uuid not null references items(id),
  location_id       uuid references locations(id),
  event_type        text not null check (event_type in
                      ('added', 'used', 'low', 'finished', 'restocked', 'adjusted', 'discarded')),
  quantity          numeric,
  unit              text,
  confidence        text not null default 'approx' check (confidence in ('exact', 'approx', 'inferred')),
  source            text not null check (source in
                      ('message', 'receipt', 'photo', 'prediction', 'shopping', 'undo')),
  expires_on        date,
  member_id         uuid references members(id),
  source_message_id uuid references messages(id),
  occurred_at       timestamptz not null default now()
);
create index inventory_events_item_idx on inventory_events (item_id, occurred_at);

create table stock (
  item_id       uuid not null references items(id) on delete cascade,
  location_id   uuid not null references locations(id) on delete cascade,
  qty_estimate  numeric,
  status        text not null check (status in ('in_stock', 'low', 'out', 'unknown')),
  expires_on    date,
  last_event_at timestamptz not null,
  primary key (item_id, location_id)
);

create table consumption_profiles (
  item_id             uuid primary key references items(id) on delete cascade,
  avg_days_to_finish  numeric,
  samples             int not null default 0,
  last_restocked_at   timestamptz,
  predicted_runout_at timestamptz,
  updated_at          timestamptz not null default now()
);

-- =========================================================
-- Shopping list
-- =========================================================
create table shopping_list_items (
  id           uuid primary key default gen_random_uuid(),
  household_id uuid not null references households(id) on delete cascade,
  item_id      uuid references items(id),
  free_text    text,
  quantity     numeric,
  unit         text,
  reason       text not null check (reason in ('explicit', 'finished', 'predicted', 'low')),
  status       text not null default 'needed' check (status in ('needed', 'bought', 'dismissed')),
  store_hint   text,
  added_by     uuid references members(id),
  added_at     timestamptz not null default now(),
  resolved_at  timestamptz,
  check (item_id is not null or free_text is not null)
);
create unique index shopping_one_active_idx
  on shopping_list_items (household_id, item_id) where status = 'needed' and item_id is not null;

-- =========================================================
-- Calendar and reminders
-- =========================================================
create table events (
  id                uuid primary key default gen_random_uuid(),
  household_id      uuid not null references households(id) on delete cascade,
  title             text not null,
  kind              text not null check (kind in ('appointment', 'activity', 'task')),
  starts_at         timestamptz not null,
  ends_at           timestamptz,
  rrule             text,
  exdates           date[] not null default '{}',     -- skipped occurrences (household-local dates)
  location          text,
  participant_ids   uuid[] not null default '{}',
  remind_before_minutes int[] not null default '{1440,60}',
  notes             text,
  status            text not null default 'active' check (status in ('active', 'cancelled')),
  source_message_id uuid references messages(id),
  created_by        uuid references members(id),
  created_at        timestamptz not null default now()
);

create table reminders (
  id           uuid primary key default gen_random_uuid(),
  household_id uuid not null references households(id) on delete cascade,
  event_id     uuid references events(id) on delete cascade,
  target       text not null check (target in ('member', 'household')),
  member_id    uuid references members(id),
  text         text not null,
  fire_at      timestamptz not null,
  rrule        text,                                  -- standalone repeating reminders only
  urgency      text not null default 'normal' check (urgency in ('low', 'normal', 'high')),
  status       text not null default 'scheduled' check (status in ('scheduled', 'sent', 'acked', 'cancelled')),
  sent_at      timestamptz,
  unique (event_id, fire_at)
);
create index reminders_due_idx on reminders (fire_at) where status = 'scheduled';

-- =========================================================
-- Presence
-- =========================================================
create table places (
  id           uuid primary key default gen_random_uuid(),
  household_id uuid not null references households(id) on delete cascade,
  name         text not null,                         -- must match the Shortcut payload
  kind         text not null check (kind in ('home', 'store', 'school', 'clinic', 'other')),
  lat          double precision,
  lng          double precision,
  unique (household_id, name)
);

create table presence_events (
  id          uuid primary key default gen_random_uuid(),
  member_id   uuid not null references members(id) on delete cascade,
  place_id    uuid not null references places(id) on delete cascade,
  event       text not null check (event in ('enter', 'exit')),
  source      text not null default 'shortcut',
  occurred_at timestamptz not null default now()
);

create table nudge_log (
  household_id uuid not null references households(id) on delete cascade,
  dedupe_key   text not null,
  sent_at      timestamptz not null default now(),
  primary key (household_id, dedupe_key)
);

-- =========================================================
-- Agent actions (undo), outbox, job runs
-- =========================================================
create table agent_actions (
  id           uuid primary key default gen_random_uuid(),
  household_id uuid not null references households(id) on delete cascade,
  member_id    uuid references members(id),
  message_id   uuid references messages(id),
  source       text not null default 'agent' check (source in ('agent', 'dashboard')),
  tool         text not null,                         -- tool or service action name
  args         jsonb not null,
  result       text not null,
  inverse      jsonb not null default '[]',           -- list of {op, table, rows|ids}
  touched      jsonb not null default '[]',           -- [{table, id}] for undo conflict checks
  created_at   timestamptz not null default now(),
  undone_at    timestamptz
);
create index agent_actions_recent_idx on agent_actions (household_id, member_id, created_at desc);

create table outbox (
  id                  uuid primary key default gen_random_uuid(),
  household_id        uuid not null references households(id) on delete cascade,
  target              text not null check (target in ('thread', 'member', 'household')),
  thread_id           uuid references threads(id),
  member_id           uuid references members(id),
  text                text,
  react_emoji         text,                           -- 'ack' resolves to the adapter's ack emoji
  reply_to_message_id uuid references messages(id),
  urgency             text not null default 'normal' check (urgency in ('low', 'normal', 'high')),
  respect_quiet_hours boolean not null default true,
  send_after          timestamptz not null default now(),
  status              text not null default 'pending'
                      check (status in ('pending', 'sending', 'sent', 'failed', 'cancelled', 'simulated')),
  attempts            int not null default 0,
  last_error          text,
  channel_used        text,
  external_id         text,
  dedupe_key          text,
  created_at          timestamptz not null default now(),
  sent_at             timestamptz,
  check (text is not null or react_emoji is not null),
  unique (household_id, dedupe_key)
);
create index outbox_due_idx on outbox (send_after) where status = 'pending';

create table job_runs (
  job          text not null,
  household_id uuid not null references households(id) on delete cascade,
  run_key      text not null,                         -- e.g. '2026-10-05' or '2026-W41'
  ran_at       timestamptz not null default now(),
  primary key (job, household_id, run_key)
);
