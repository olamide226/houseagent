"""Two-way calendar sync (ADR 0038): an adult's connected calendar, what each household event is
on it, events that last all day, and `calendar` as a source of logged changes."""
from alembic import op

revision = "0003"
down_revision = "0002"

UPGRADE = [
    # Not NOT NULL: undo puts an event back from the copy taken before a change, and a copy taken
    # before this migration has no value for it. Null reads as "not all day".
    "alter table events add column all_day boolean default false",
    "alter table agent_actions drop constraint agent_actions_source_check",
    """alter table agent_actions add constraint agent_actions_source_check
         check (source in ('agent', 'dashboard', 'calendar'))""",
    """create table calendar_connections (
         id             uuid primary key default gen_random_uuid(),
         household_id   uuid not null references households(id) on delete cascade,
         member_id      uuid not null unique references members(id) on delete cascade,  -- one per adult
         provider       text not null check (provider in ('google', 'apple')),
         account        text,                         -- the address signed in with, as the page shows it
         secret         text not null,                -- the sign-in, encrypted with CALENDAR_SECRET_KEY
         status         text not null default 'choosing' check (status in ('choosing', 'active', 'broken')),
         choices        jsonb not null default '[]',  -- [{id, name}]: the calendars to pick from
         calendar_id    text,                         -- the one calendar shared with the household
         calendar_name  text,
         sync_token     text,                         -- the provider's place in its list of changes
         next_pull_at   timestamptz,                  -- when to ask the provider for changes again
         leased_until   timestamptz,                  -- a worker is on it until then
         last_synced_at timestamptz,
         failures       int not null default 0,       -- in a row; each one waits longer
         last_error     text,
         created_at     timestamptz not null default now()
       )""",
    """create unique index calendar_connections_calendar_idx
         on calendar_connections (household_id, provider, calendar_id) where calendar_id is not null""",
    """create table calendar_links (
         id            uuid primary key default gen_random_uuid(),
         connection_id uuid not null references calendar_connections(id) on delete cascade,
         event_id      uuid references events(id) on delete set null,  -- null: the event is gone, remove it there too
         remote_id     text not null,
         remote_etag   text,
         synced        jsonb not null,                -- the event as both sides last agreed on it
         instances     date[] not null default '{}',  -- dates the calendar itself holds as changed occurrences
         unique (connection_id, remote_id)
       )""",
    "create index calendar_links_event_idx on calendar_links (event_id)",
]
DOWNGRADE = [
    "drop table calendar_links",
    "drop table calendar_connections",
    "delete from agent_actions where source = 'calendar'",
    "alter table agent_actions drop constraint agent_actions_source_check",
    "alter table agent_actions add constraint agent_actions_source_check check (source in ('agent', 'dashboard'))",
    "alter table events drop column all_day",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
