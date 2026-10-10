"""Household jobs: a standalone reminder that is to be done, not only said (ADR 0037).

A job is a `reminders` row with a `kind`. The new columns are null on every plain reminder, so
nothing that reads those changes. None of them is NOT NULL: undo puts a row back from the copy
taken before a change, and a copy taken before this migration has no value for them.
"""
from alembic import op

revision = "0002"
down_revision = "0001"

COLUMNS = ("kind", "due_at", "all_day", "lead_days", "asked_at", "done_at", "done_by")


def upgrade() -> None:
    op.execute("""
        alter table reminders
          add column kind      text check (kind in ('job', 'date')),  -- null: a plain reminder
          add column due_at    timestamptz,                           -- the occurrence it is waiting on
          add column all_day   boolean,                               -- due on a day, with no time
          add column lead_days int,                                   -- how long before to say it is coming
          add column asked_at  timestamptz,                           -- first nudge about this occurrence
          add column done_at   timestamptz,                           -- when it was last ticked off
          add column done_by   uuid references members(id),
          add check (kind is null or due_at is not null)
    """)


def downgrade() -> None:
    op.execute("delete from reminders where kind is not null")
    for column in COLUMNS:
        op.execute(f"alter table reminders drop column {column}")
