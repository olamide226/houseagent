"""The address a shop link was made for, and a pending offer to replace it (ADR 0042).

`members.presence_link_base` is the PUBLIC_BASE_URL a member's presence token was made under; null
when it is not known. `members.presence_renew_offered_at` is when the word shops last offered that
member a new link, which a yes then takes up.

A link made before this migration has no recorded address. Most were sent in chat, so the message
that sent one still holds it: the address is read back from there, for the link whose token hashes
to the one stored. Nothing is copied from a message but the address. A link only ever shown on the
Settings page stays null, and is treated as possibly made for an old address (docs/presence.md).
"""
from alembic import op

revision = "0005"
down_revision = "0004"

# The address and the token of every presence link in what was sent. Tokens are 32 random bytes,
# URL-safe base64: 43 characters. Each member's link gets the address of the latest message whose
# token hashes to theirs; any other member keeps null.
LINKS = r"(https?://[^\s]+?)/presence/([A-Za-z0-9_-]{43})"
BACKFILL = f"""
    with sent as (
      select text, created_at from messages where direction = 'out' and text like '%/presence/%'
      union all
      select text, created_at from outbox where text like '%/presence/%'
    ), links as (
      select found[1] as base, encode(sha256(convert_to(found[2], 'UTF8')), 'hex') as token_hash, created_at
      from sent, regexp_matches(text, '{LINKS}', 'g') as found
    )
    update members m set presence_link_base = (
      select l.base from links l where l.token_hash = m.presence_token_hash order by l.created_at desc limit 1)
    where m.presence_token_hash is not null and m.presence_link_base is null"""


def upgrade() -> None:
    op.execute("alter table members add column presence_link_base text")
    op.execute("alter table members add column presence_renew_offered_at timestamptz")
    op.execute(BACKFILL)


def downgrade() -> None:
    op.execute("alter table members drop column if exists presence_renew_offered_at")
    op.execute("alter table members drop column if exists presence_link_base")
