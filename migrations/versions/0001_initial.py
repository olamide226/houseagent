"""Initial schema: executes schema.sql verbatim."""
from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None

SCHEMA = Path(__file__).resolve().parents[2] / "schema.sql"

TABLES = [
    "job_runs", "outbox", "agent_actions", "nudge_log", "presence_events", "places", "reminders",
    "events", "shopping_list_items", "consumption_profiles", "stock", "inventory_events", "items",
    "locations", "household_facts", "messages", "threads", "channel_identities", "login_tokens",
    "members", "households",
]


def upgrade() -> None:
    # schema.sql is many statements; asyncpg only accepts that through its simple-query
    # path, so hand the file to the driver connection untouched.
    raw = op.get_bind().connection.dbapi_connection
    raw.run_async(lambda conn: conn.execute(SCHEMA.read_text()))  # type: ignore[union-attr]


def downgrade() -> None:
    for table in TABLES:
        op.execute(f"drop table if exists {table} cascade")
