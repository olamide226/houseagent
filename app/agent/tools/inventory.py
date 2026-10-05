"""Inventory tools: log_inventory, query_inventory."""
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.actions import record
from app.agent.base import Ctx
from app.agent.resolve import Ambiguous, match_item, match_location, resolve_item, resolve_location
from app.services import inventory


class InventoryChange(BaseModel):
    item: str = Field(description="Natural name, e.g. 'eggs', 'Indomie', 'chicken thighs'")
    action: Literal["added", "used", "low", "finished", "restocked", "adjusted", "discarded"] = Field(
        description="low = running low; finished = none left; adjusted = absolute count seen (photos)")
    quantity: float | None = Field(None, description="Only if stated or clearly visible. Never guess.")
    unit: str | None = Field(None, description="e.g. 'pints', 'kg', 'packs'")
    location: str | None = Field(
        None, description="fridge, freezer, store, or a custom location. Omit for usual place.")
    expires_on: date | None = None


class LogInventory(BaseModel):
    changes: list[InventoryChange] = Field(min_length=1)
    source: Literal["message", "receipt", "photo"] = "message"


async def log_inventory(ctx: Ctx, args: LogInventory) -> str:
    """Record food and household stock that came in, got used, is running low or ran out.
    Batch every change from the turn into one call. Unknown items are created (NEW:).
    Ambiguous names are skipped and reported (AMBIGUOUS:); other changes still apply.
    Finished staples and anything running low are added to the shopping list (NOTE:)."""
    async with record(ctx, "log_inventory", args) as rec:
        for change in args.changes:
            location_id = None
            if change.location:
                location = await resolve_location(ctx.conn, ctx.household_id, change.location)
                if isinstance(location, Ambiguous):
                    rec.lines.append(f"AMBIGUOUS: location '{change.location}' could be "
                                     f"{', '.join(location.options)}. Nothing recorded for {change.item}.")
                    continue
                location_id = location.id
            item = await resolve_item(ctx.conn, ctx.household_id, change.item, location_id)
            if isinstance(item, Ambiguous):
                rec.lines.append(f"AMBIGUOUS: '{change.item}' could be {', '.join(item.options)}")
                continue
            if item.created:
                rec.lines.append(f"NEW: {item.name}")
            await inventory.apply_change(
                rec,
                inventory.Change(
                    item.id, change.action,
                    None if change.quantity is None else Decimal(str(change.quantity)),
                    change.unit, location_id, change.expires_on,
                ),
                args.source,
            )
    return rec.result


class QueryInventory(BaseModel):
    item: str | None = None
    location: str | None = None
    status: list[Literal["in_stock", "low", "out", "unknown"]] | None = None
    expiring_within_days: int | None = Field(None, ge=0, le=60)


async def query_inventory(ctx: Ctx, args: QueryInventory) -> str:
    """Answer 'do we have X', 'what's in the freezer', 'what's running low', 'what expires soon'."""
    item_id = location_id = None
    if args.item:
        item = await match_item(ctx.conn, ctx.household_id, args.item)
        if item is None:
            return f"OK: nothing recorded for {args.item}"
        if isinstance(item, Ambiguous):
            return f"AMBIGUOUS: '{args.item}' could be {', '.join(item.options)}"
        item_id = item.id
    if args.location:
        location = await match_location(ctx.conn, ctx.household_id, args.location)
        if location is None:
            return f"OK: no location called {args.location}"
        if isinstance(location, Ambiguous):
            return f"AMBIGUOUS: location '{args.location}' could be {', '.join(location.options)}"
        location_id = location.id
    rows = await inventory.stock_rows(
        ctx.conn, ctx.household_id, item_id=item_id, location_id=location_id,
        statuses=list(args.status) if args.status else None, expiring_within_days=args.expiring_within_days,
    )
    if not rows:
        return "OK: nothing matches"
    return "\n".join(inventory.stock_line(row) for row in rows)
