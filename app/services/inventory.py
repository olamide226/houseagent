"""Inventory write path, shared by agent tools and the dashboard.

`stock` is only ever written here, in the same transaction that appends the
`inventory_events` row, and the deterministic side effects of spec section 9.4 run here.
"""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.agent import stock
from app.agent.actions import Recorder
from app.agent.base import ToolError
from app.db import execute, fetch_all, fetch_one, fetch_val
from app.services import shopping

DEFAULT_LOCATIONS = {
    "fridge": ["refrigerator"],
    "freezer": ["deep freezer", "chest freezer"],
    "store": ["pantry", "cupboard", "store cupboard", "larder"],
}

# Events that still count: undo markers and the events of undone actions are history only.
LIVE_EVENTS = """inventory_events e
  where e.source <> 'undo' and not exists (
    select 1 from agent_actions a, jsonb_array_elements(a.touched) t
    where a.household_id = e.household_id and a.undone_at is not null
      and t->>'table' = 'inventory_events' and t->>'id' = e.id::text)"""


@dataclass(frozen=True)
class Change:
    item_id: str
    action: str
    quantity: Decimal | None = None
    unit: str | None = None
    location_id: str | None = None   # None: the item's usual place
    expires_on: date | None = None


def quantity_text(quantity: Decimal | None, unit: str | None) -> str:
    if quantity is None:
        return ""
    number = f"{quantity.normalize():f}"
    return f"{number} {unit}" if unit else number


# ---------------------------------------------------------------- items and locations
async def create_location(conn: AsyncConnection, household_id: str, name: str,
                          aliases: list[str] | None = None) -> str:
    return str(await fetch_val(
        conn,
        "insert into locations (household_id, name, aliases) values (:h, :name, :aliases) returning id",
        h=household_id, name=name, aliases=aliases or [],
    ))


async def add_location_alias(conn: AsyncConnection, location_id: str, alias: str) -> None:
    await execute(conn, "update locations set aliases = array_append(aliases, :alias) "
                        "where id = :id and not :alias = any(aliases)", id=location_id, alias=alias)


async def create_item(conn: AsyncConnection, household_id: str, name: str,
                      location_id: str | None = None, *, is_staple: bool = False) -> str:
    """A new item with no location goes to `store`."""
    return str(await fetch_val(
        conn,
        """insert into items (household_id, canonical_name, default_location_id, is_staple)
           values (:h, :name, coalesce(:location, (select id from locations
                                                   where household_id = :h and name = 'store')), :staple)
           returning id""",
        h=household_id, name=name, location=location_id, staple=is_staple,
    ))


async def uncategorised(conn: AsyncConnection, household_id: str, limit: int) -> list[str]:
    rows = await fetch_all(conn, "select canonical_name from items where household_id = :h and category is null "
                                 "order by created_at, canonical_name limit :limit", h=household_id, limit=limit)
    return [row["canonical_name"] for row in rows]


async def set_categories(conn: AsyncConnection, household_id: str, categories: dict[str, str]) -> int:
    """Give items that still have no category the one chosen for them, by name."""
    done = 0
    for name, category in categories.items():
        done += await execute(conn, "update items set category = :category where household_id = :h "
                                    "and canonical_name = :name and category is null",
                              category=category, h=household_id, name=name)
    return done


async def add_item_alias(conn: AsyncConnection, item_id: str, alias: str) -> None:
    await execute(conn, "update items set aliases = array_append(aliases, :alias) "
                        "where id = :id and not :alias = any(aliases)", id=item_id, alias=alias)


async def update_item(rec: Recorder, item_id: str, *, aliases: list[str], is_staple: bool,
                      low_threshold: Decimal | None, default_location_id: str | None) -> None:
    item = await _item(rec, item_id)
    await rec.before("items", id=item_id)
    await execute(
        rec.ctx.conn,
        """update items set aliases = :aliases, is_staple = :staple, low_threshold = :threshold,
                            default_location_id = coalesce(:location, default_location_id)
           where id = :id""",
        id=item_id, aliases=aliases, staple=is_staple, threshold=low_threshold, location=default_location_id,
    )
    rec.lines.append(f"OK: {item['canonical_name']} updated")


async def mark_staple(rec: Recorder, item_id: str) -> None:
    """Something the household always keeps in: it goes on the list by itself when it runs out."""
    item = await _item(rec, item_id)
    if not item["is_staple"]:
        await rec.before("items", id=item_id)
        await execute(rec.ctx.conn, "update items set is_staple = true where id = :id", id=item_id)
    rec.lines.append(f"OK: {item['canonical_name']} is a staple")


async def merge_items(rec: Recorder, keep_id: str, duplicate_id: str) -> None:
    """Fold `duplicate` into `keep`: events, stock, list entries and aliases, in one transaction.
    Not undoable: the duplicate's rows are rewritten in place."""
    if keep_id == duplicate_id:
        raise ToolError("pick two different items to merge")
    keep, duplicate = await _item(rec, keep_id), await _item(rec, duplicate_id)
    conn = rec.ctx.conn
    await execute(conn, "update inventory_events set item_id = :keep where item_id = :dup",
                  keep=keep_id, dup=duplicate_id)
    # Where both have stock in one location the kept row wins; quantities add when both are known.
    await execute(
        conn,
        """update stock k set qty_estimate = k.qty_estimate + d.qty_estimate
           from stock d where k.item_id = :keep and d.item_id = :dup and d.location_id = k.location_id
             and k.qty_estimate is not null and d.qty_estimate is not null""",
        keep=keep_id, dup=duplicate_id,
    )
    await execute(
        conn,
        """update stock set item_id = :keep where item_id = :dup and location_id not in
             (select location_id from stock where item_id = :keep)""",
        keep=keep_id, dup=duplicate_id,
    )
    await execute(
        conn,
        """update shopping_list_items set status = 'dismissed', resolved_at = clock_timestamp()
           where item_id = :dup and status = 'needed'
             and exists (select 1 from shopping_list_items where item_id = :keep and status = 'needed')""",
        keep=keep_id, dup=duplicate_id,
    )
    await execute(conn, "update shopping_list_items set item_id = :keep where item_id = :dup",
                  keep=keep_id, dup=duplicate_id)
    aliases = sorted({*keep["aliases"], *duplicate["aliases"], duplicate["canonical_name"].lower()}
                     - {keep["canonical_name"].lower()})
    await execute(conn, "update items set aliases = :aliases where id = :keep", aliases=aliases, keep=keep_id)
    await execute(conn, "delete from items where id = :dup", dup=duplicate_id)
    rec.appended("items", keep_id)
    rec.lines.append(f"OK: merged {duplicate['canonical_name']} into {keep['canonical_name']}")


async def _item(rec: Recorder, item_id: str) -> dict[str, Any]:
    item = await fetch_one(
        rec.ctx.conn, "select * from items where id = :id and household_id = :h for update",
        id=item_id, h=rec.ctx.household_id,
    )
    if item is None:
        raise ToolError("unknown item")
    return item


# ---------------------------------------------------------------- stock changes
async def apply_change(rec: Recorder, change: Change, source: str) -> None:
    """Append one inventory event, project it onto `stock`, then run the side-effect rules."""
    ctx, conn = rec.ctx, rec.ctx.conn
    item = await _item(rec, change.item_id)
    name = item["canonical_name"]
    location_id = change.location_id or await _usual_location(conn, item)
    location = await fetch_val(conn, "select name from locations where id = :id and household_id = :h",
                               id=location_id, h=ctx.household_id)
    if location is None:
        raise ToolError("unknown location")

    await rec.before("stock", item_id=change.item_id, location_id=location_id)
    current = await fetch_one(
        conn, "select qty_estimate, status, expires_on from stock where item_id = :i and location_id = :l",
        i=change.item_id, l=location_id,
    )
    previous = None
    if current:
        previous = stock.StockRow(current["qty_estimate"], current["status"], current["expires_on"])
    after = stock.apply(previous, stock.StockEvent(change.action, change.quantity, change.expires_on),
                        stock.Item(item["low_threshold"]))

    event = await fetch_one(
        conn,
        """insert into inventory_events
             (household_id, item_id, location_id, event_type, quantity, unit, source, expires_on,
              member_id, source_message_id, occurred_at)
           values (:h, :item, :location, :type, :quantity, :unit, :source, :expires, :member, :message,
                   clock_timestamp())
           returning id, occurred_at""",
        h=ctx.household_id, item=change.item_id, location=location_id, type=change.action,
        quantity=change.quantity, unit=change.unit, source=source, expires=change.expires_on,
        member=ctx.member_id, message=ctx.message_id,
    )
    assert event is not None
    rec.appended("inventory_events", event["id"])
    await execute(
        conn,
        """insert into stock (item_id, location_id, qty_estimate, status, expires_on, last_event_at)
           values (:item, :location, :qty, :status, :expires, :at)
           on conflict (item_id, location_id) do update set
             qty_estimate = excluded.qty_estimate, status = excluded.status,
             expires_on = excluded.expires_on, last_event_at = excluded.last_event_at""",
        item=change.item_id, location=location_id, qty=after.qty, status=after.status,
        expires=after.expires_on, at=event["occurred_at"],
    )
    if change.unit and not item["default_unit"]:
        await execute(conn, "update items set default_unit = :unit where id = :id",
                      unit=change.unit, id=change.item_id)

    amount = quantity_text(change.quantity, change.unit)
    rec.lines.append(f"OK: {name} {change.action}{', ' + amount if amount else ''} ({location})")

    if change.action in ("added", "restocked"):
        if await shopping.resolve_entry(rec, change.item_id, "bought"):
            rec.lines.append(f"NOTE: {name} ticked off the shopping list")
    elif change.action == "low":
        if await shopping.add_entry(rec, change.item_id, "low"):
            rec.lines.append(f"NOTE: {name} added to shopping list")
    elif after.status == "out" and change.action in ("finished", "discarded"):
        staple = item["is_staple"]
        if not staple and await _completed_cycles(conn, change.item_id) >= 2:
            await rec.before("items", id=change.item_id)
            await execute(conn, "update items set is_staple = true where id = :id", id=change.item_id)
            rec.lines.append(f"NOTE: {name} is now a staple")
            staple = True
        if staple and await shopping.add_entry(rec, change.item_id, "finished"):
            rec.lines.append(f"NOTE: {name} added to shopping list")


async def _usual_location(conn: AsyncConnection, item: dict[str, Any]) -> str:
    """Where the item actually is if it is stocked in exactly one place, else its default."""
    stocked = await fetch_all(conn, "select location_id from stock where item_id = :i", i=item["id"])
    if len(stocked) == 1:
        return str(stocked[0]["location_id"])
    return str(item["default_location_id"] or await fetch_val(
        conn, "select id from locations where household_id = :h and name = 'store'", h=item["household_id"]
    ))


async def _completed_cycles(conn: AsyncConnection, item_id: str) -> int:
    """Restock-to-finished cycles so far; two of them make an item a staple."""
    events = await fetch_all(
        conn,
        f"select e.event_type, e.quantity from {LIVE_EVENTS} and e.item_id = :item "
        "order by e.occurred_at, e.id",
        item=item_id,
    )
    cycles, stocked = 0, False
    for event in events:
        if event["event_type"] in ("added", "restocked"):
            stocked = True
        elif stocked and (event["event_type"] == "finished"
                          or (event["event_type"] == "discarded" and event["quantity"] is None)):
            cycles, stocked = cycles + 1, False
    return cycles


async def rebuild_stock(conn: AsyncConnection, household_id: str) -> None:
    """Regenerate `stock` from scratch by replaying live events in order."""
    events = await fetch_all(
        conn,
        f"""select e.item_id, e.location_id, e.event_type, e.quantity, e.expires_on, e.occurred_at,
                   (select low_threshold from items where id = e.item_id) as low_threshold
            from {LIVE_EVENTS} and e.household_id = :h and e.location_id is not null
            order by e.occurred_at, e.id""",
        h=household_id,
    )
    rows: dict[tuple[str, str], tuple[stock.StockRow, Any]] = {}
    for e in events:
        key = (e["item_id"], e["location_id"])
        previous = rows[key][0] if key in rows else None
        rows[key] = (
            stock.apply(previous, stock.StockEvent(e["event_type"], e["quantity"], e["expires_on"]),
                        stock.Item(e["low_threshold"])),
            e["occurred_at"],
        )
    await execute(conn, "delete from stock where item_id in (select id from items where household_id = :h)",
                  h=household_id)
    for (item_id, location_id), (row, at) in rows.items():
        await execute(
            conn,
            """insert into stock (item_id, location_id, qty_estimate, status, expires_on, last_event_at)
               values (:item, :location, :qty, :status, :expires, :at)""",
            item=item_id, location=location_id, qty=row.qty, status=row.status, expires=row.expires_on, at=at,
        )


# ---------------------------------------------------------------- reads
async def stock_rows(
    conn: AsyncConnection, household_id: str, *, item_id: str | None = None, location_id: str | None = None,
    statuses: list[str] | None = None, expiring_within_days: int | None = None, today: date | None = None,
) -> list[dict[str, Any]]:
    """Stock rows, filtered. `today` is the household-local date that "expiring within" counts from."""
    return await fetch_all(
        conn,
        """select i.id as item_id, i.canonical_name as item, i.default_unit as unit, i.is_staple,
                  l.id as location_id, l.name as location, s.qty_estimate, s.status, s.expires_on,
                  s.last_event_at
           from stock s join items i on i.id = s.item_id join locations l on l.id = s.location_id
           where i.household_id = :h
             and (cast(:item as uuid) is null or i.id = :item)
             and (cast(:location as uuid) is null or l.id = :location)
             and (cast(:statuses as text[]) is null or s.status = any(:statuses))
             and (cast(:days as int) is null
                  or (s.expires_on is not null
                      and s.expires_on <= coalesce(cast(:today as date), current_date) + cast(:days as int)))
           order by l.name, i.canonical_name""",
        h=household_id, item=item_id, location=location_id, statuses=statuses, days=expiring_within_days,
        today=today,
    )


def stock_line(row: dict[str, Any]) -> str:
    amount = quantity_text(row["qty_estimate"], row["unit"]) if row["status"] != "out" else ""
    line = f"{row['item']}: {row['status'].replace('_', ' ')}{', ' + amount if amount else ''} ({row['location']})"
    if row["expires_on"]:
        line += f", expires {row['expires_on']:%-d %b}"
    return line
