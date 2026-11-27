"""Model outages: when the model stopped answering, why, and whether the admins were told (ADR 0043).

One row per outage of the installation's model. At most one is open (`ended_at` null): the
partial unique index is what makes two workers that fail at the same moment share it. `cause` is
a key app/services/model_health.py words for people; `error_type` is the provider's own name for
the error (a type or a code, never its message), shown to admins on the System page.
"""
from alembic import op

revision = "0006"
down_revision = "0005"


def upgrade() -> None:
    op.execute("""
        create table model_outages (
          id             uuid primary key default gen_random_uuid(),
          kind           text not null check (kind in ('config', 'transient')),
          cause          text not null,
          error_type     text,
          provider       text not null,
          started_at     timestamptz not null,
          last_failed_at timestamptz not null,
          failures       int not null default 1,
          alerted_at     timestamptz,                 -- when the admins were told; null while nobody was
          reminded_at    timestamptz,                 -- the last time they were told, first or again
          ended_at       timestamptz                  -- the first turn that worked after it
        )""")
    op.execute("create unique index model_outages_open on model_outages ((true)) where ended_at is null")
    op.execute("create index model_outages_started_idx on model_outages (started_at desc)")


def downgrade() -> None:
    op.execute("drop table if exists model_outages")
