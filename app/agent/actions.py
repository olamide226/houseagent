"""agent_actions log and undo machinery (spec section 9.3).

Every write made on behalf of a tool call or a dashboard action goes through a Recorder,
which remembers each row's prior state. That is the action's inverse; undo applies it.
"""
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from pydantic import BaseModel

from app.agent.base import Ctx, ToolError
from app.db import execute, fetch_all, fetch_one, fetch_val, jsonb

# Tables whose rows undo may restore or delete, with their primary keys.
PK: dict[str, tuple[str, ...]] = {
    "stock": ("item_id", "location_id"),
    "shopping_list_items": ("id",),
    "items": ("id",),
}
_IDENT = re.compile(r"^[a-z_]+$")
UNDO_WINDOW_HOURS = 24


class Recorder:
    def __init__(self, ctx: Ctx) -> None:
        self.ctx = ctx
        self.lines: list[str] = []
        self.touched: list[dict[str, str]] = []
        self._before: dict[tuple[str, str], dict[str, Any] | None] = {}

    @property
    def result(self) -> str:
        return "\n".join(self.lines)

    async def before(self, table: str, **pk: str) -> None:
        """Call before changing or creating a row; the first call per row captures its prior state."""
        key = ":".join(pk[column] for column in PK[table])
        if (table, key) in self._before:
            return
        where = " and ".join(f"{column} = :{column}" for column in PK[table])
        self._before[(table, key)] = await fetch_val(
            self.ctx.conn, f"select to_jsonb(t) from {table} t where {where}", **pk
        )
        self.touched.append({"table": table, "id": key})

    def created(self, table: str, row_id: str) -> None:
        """A row this action inserted under a generated id."""
        self._before[(table, row_id)] = None
        self.touched.append({"table": table, "id": row_id})

    def appended(self, table: str, row_id: str) -> None:
        """An append-only row (inventory_events): tracked, never restored or deleted."""
        self.touched.append({"table": table, "id": row_id})

    def inverse(self) -> list[dict[str, Any]]:
        restore: dict[str, list[dict[str, Any]]] = {}
        delete: dict[str, list[str]] = {}
        for (table, key), row in self._before.items():
            if row is None:
                delete.setdefault(table, []).append(key)
            else:
                restore.setdefault(table, []).append(row)
        return [
            *({"op": "restore_rows", "table": t, "rows": rows} for t, rows in restore.items()),
            *({"op": "delete_rows", "table": t, "ids": ids} for t, ids in delete.items()),
        ]

    async def save(self, tool: str, args: dict[str, Any]) -> None:
        if not self.touched:
            return  # nothing was written, so there is nothing to show or undo
        ctx = self.ctx
        await execute(
            ctx.conn,
            """insert into agent_actions
                 (household_id, member_id, message_id, source, tool, args, result, inverse, touched, created_at)
               values (:household, :member, :message, :source, :tool, cast(:args as jsonb), :result,
                       cast(:inverse as jsonb), cast(:touched as jsonb), clock_timestamp())""",
            household=ctx.household_id, member=ctx.member_id, message=ctx.message_id, source=ctx.source,
            tool=tool, args=jsonb(args), result=self.result,
            inverse=jsonb(self.inverse()), touched=jsonb(self.touched),
        )


@asynccontextmanager
async def record(ctx: Ctx, tool: str, args: BaseModel | dict[str, Any]) -> AsyncIterator[Recorder]:
    """Collect one action's writes and log it to agent_actions when the block succeeds."""
    recorder = Recorder(ctx)
    yield recorder
    await recorder.save(tool, args.model_dump(mode="json") if isinstance(args, BaseModel) else args)


async def undo_last(ctx: Ctx, n: int) -> list[str]:
    """Undo this member's newest `n` actions from the past 24 hours, newest first."""
    actions = await fetch_all(
        ctx.conn,
        """select id from agent_actions
           where household_id = :household and member_id is not distinct from :member
             and undone_at is null and inverse <> '[]'
             and created_at > clock_timestamp() - make_interval(hours => :hours)
           order by created_at desc limit :n""",
        household=ctx.household_id, member=ctx.member_id, hours=UNDO_WINDOW_HOURS, n=n,
    )
    if not actions:
        raise ToolError("nothing to undo in the last 24 hours")
    lines: list[str] = []
    for action in actions:
        try:
            lines.append(await undo_action(ctx, action["id"]))
        except ToolError as exc:
            if not lines:
                raise
            lines.append(f"ERROR: {exc}")
            break
    return lines


async def undo_action(ctx: Ctx, action_id: str) -> str:
    action = await fetch_one(
        ctx.conn,
        """select id, tool, result, inverse, touched, created_at from agent_actions
           where id = :id and household_id = :household and undone_at is null for update""",
        id=action_id, household=ctx.household_id,
    )
    if action is None:
        raise ToolError("that action was already undone")
    later = await fetch_one(
        ctx.conn,
        """select b.tool from agent_actions b
           where b.household_id = :household and b.undone_at is null and b.id <> :id
             and b.created_at > :created
             and exists (select 1 from jsonb_array_elements(b.touched) theirs
                         join jsonb_array_elements(cast(:touched as jsonb)) ours on theirs = ours
                         where theirs->>'table' <> 'inventory_events')
           order by b.created_at desc limit 1""",
        household=ctx.household_id, id=action["id"], created=action["created_at"],
        touched=jsonb(action["touched"]),
    )
    if later is not None:
        raise ToolError(
            f"can't undo {action['tool']}: a later {later['tool']} changed the same things. Undo that first."
        )

    for op in action["inverse"]:
        table = op["table"]
        pk = PK[table]
        if op["op"] == "restore_rows":
            for row in op["rows"]:
                await _restore(ctx, table, pk, row)
        else:
            where = " and ".join(f"{column} = :{column}" for column in pk)
            for key in op["ids"]:
                await execute(ctx.conn, f"delete from {table} where {where}",
                              **dict(zip(pk, key.split(":"), strict=True)))
    await _log_stock_restores(ctx, action["inverse"])
    await execute(ctx.conn, "update agent_actions set undone_at = clock_timestamp() where id = :id",
                  id=action["id"])
    return f"OK: undid {action['tool']} ({action['result'].splitlines()[0]})"


async def _restore(ctx: Ctx, table: str, pk: tuple[str, ...], row: dict[str, Any]) -> None:
    columns = [c for c in row if c not in pk]
    if not all(_IDENT.fullmatch(c) for c in row):
        raise ToolError("stored undo data is malformed")
    await execute(
        ctx.conn,
        f"""insert into {table} select * from jsonb_populate_record(null::{table}, cast(:row as jsonb))
            on conflict ({", ".join(pk)}) do update set {", ".join(f"{c} = excluded.{c}" for c in columns)}""",
        row=jsonb(row),
    )


async def _log_stock_restores(ctx: Ctx, inverse: list[dict[str, Any]]) -> None:
    """inventory_events is append-only: record each reverted stock row as an `adjusted` undo event."""
    for op in inverse:
        if op["table"] != "stock":
            continue
        rows = op["rows"] if op["op"] == "restore_rows" else [
            dict(zip(PK["stock"], key.split(":"), strict=True)) for key in op["ids"]
        ]
        for row in rows:
            await execute(
                ctx.conn,
                """insert into inventory_events
                     (household_id, item_id, location_id, event_type, quantity, source, member_id,
                      source_message_id, occurred_at)
                   values (:household, :item, :location, 'adjusted', :quantity, 'undo', :member,
                           :message, clock_timestamp())""",
                household=ctx.household_id, item=row["item_id"], location=row["location_id"],
                quantity=row.get("qty_estimate"), member=ctx.member_id, message=ctx.message_id,
            )
