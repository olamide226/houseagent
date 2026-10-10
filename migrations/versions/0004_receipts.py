"""Receipts: what a receipt that was read cost, beside the stock it restocked (ADR 0040).

`receipts` holds the shop, the date and the printed total; `receipt_lines` the price of each
line that was logged. `households.currency` says what the amounts are in.
"""
from alembic import op

revision = "0004"
down_revision = "0003"


def upgrade() -> None:
    op.execute("alter table households add column currency text not null default 'GBP'")
    op.execute("""
        create table receipts (
          id                uuid primary key default gen_random_uuid(),
          household_id      uuid not null references households(id) on delete cascade,
          shop              text,                       -- null when it could not be read
          purchased_on      date not null,              -- the date printed, else the household-local day it was sent
          total             numeric(12,2) check (total >= 0),   -- as printed; null when the receipt shows none
          currency          text not null,              -- the household's when it was read
          status            text not null default 'kept' check (status in ('kept', 'removed')),
          member_id         uuid references members(id),
          source_message_id uuid references messages(id),
          created_at        timestamptz not null default now()
        )""")
    op.execute("create index receipts_household_day_idx on receipts (household_id, purchased_on)")
    op.execute("""
        create table receipt_lines (
          id         uuid primary key default gen_random_uuid(),
          receipt_id uuid not null references receipts(id) on delete cascade,
          item_id    uuid references items(id),
          label      text not null,                     -- the item's name when the line was read
          quantity   numeric,
          unit       text,
          price      numeric(12,2) not null check (price >= 0),   -- what the line cost, as printed
          position   int not null default 0
        )""")
    op.execute("create index receipt_lines_receipt_idx on receipt_lines (receipt_id)")
    op.execute("create index receipt_lines_item_idx on receipt_lines (item_id)")


def downgrade() -> None:
    op.execute("drop table if exists receipt_lines")
    op.execute("drop table if exists receipts")
    op.execute("alter table households drop column if exists currency")
