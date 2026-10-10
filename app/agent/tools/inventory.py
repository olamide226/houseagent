"""Inventory tools: log_inventory, query_inventory."""
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.actions import record
from app.agent.base import Ctx
from app.agent.resolve import (
    Ambiguous,
    match_item,
    match_location,
    resolve_item,
    resolve_location,
    variety_of,
)
from app.services import inventory, spending


class InventoryChange(BaseModel):
    item: str = Field(description=(
        "Natural name, e.g. 'eggs', 'Indomie', 'chicken thighs'. From a receipt line or a pack label, "
        "the everyday name the family would say: 'milk' for 'SEMI SKIMMED MILK 2 PINTS'"))
    action: Literal["added", "used", "low", "finished", "restocked", "adjusted", "discarded"] = Field(
        description="low = running low; finished = none left; adjusted = absolute count seen (photos)")
    quantity: float | None = Field(None, description="Only if stated or clearly visible. Never guess.")
    unit: str | None = Field(None, description="e.g. 'pints', 'kg', 'packs'")
    location: str | None = Field(
        None, description="fridge, freezer, store, or a custom location. Omit for usual place.")
    expires_on: date | None = None
    new_item: bool = Field(False, description=(
        "Leave false. True only after a result asked whether this is an item the household already has, "
        "and it is a different product"))
    price: float | None = Field(None, ge=0, description=(
        "Receipts only: what this line cost, exactly as printed. Empty if the receipt shows no price for it or "
        "it cannot be read. Never a price you worked out or guessed"))


class Receipt(BaseModel):
    shop: str | None = Field(None, description="The shop's name as printed")
    bought_on: date | None = Field(None, description="The date printed on it, YYYY-MM-DD")
    total: float | None = Field(None, ge=0, description=(
        "The total paid as printed, after any discount. Empty if it cannot be read: never a sum you worked out"))


class LogInventory(BaseModel):
    changes: list[InventoryChange] = Field(min_length=1)
    source: Literal["message", "receipt", "photo"] = "message"
    receipt: Receipt | None = Field(None, description=(
        "With source receipt: what is printed on it, kept for the household's spending. One call per receipt"))


async def log_inventory(ctx: Ctx, args: LogInventory) -> str:
    """Record food and household stock that came in, got used, is running low or ran out.
    Batch every change from the turn into one call. Unknown items are created (NEW:).
    Ambiguous names are skipped and reported (AMBIGUOUS:); other changes still apply.
    The shopping list follows by itself and each NOTE: says how: finished staples and anything
    running low go on it, purchases are ticked off it. Leave it at that: add what ran out yourself
    only if the person asked for it to go on the list, and do not offer to. A result of only OK:,
    NEW: and NOTE: lines is routine, not news: the reply to it is ACK, or after a photo one line
    saying what was read.
    From a receipt, also give each line's printed price and the receipt's shop, date and total:
    they are kept for the household's spending, and only what is printed counts."""
    priced: list[spending.Line] = []
    source = args.source
    if source != "receipt" and any(change.price is not None for change in args.changes) and (
            await spending.started(ctx.conn, ctx.household_id, ctx.message_id)):
        # The rest of a receipt this turn is already reading: a model that had to correct a name
        # logs it again in a second call and often leaves the source out, or calls it a photo.
        source = "receipt"
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
            if not change.new_item and (known := await variety_of(ctx.conn, ctx.household_id, change.item)):
                rec.lines.append(
                    f"ERROR: '{change.item}' not recorded. The household already has {', '.join(known)}. Decide "
                    "which this is without asking: the same thing, then log it again under that name; a "
                    "different product, then log it again with new_item true")
                continue
            item = await resolve_item(ctx.conn, ctx.household_id, change.item, location_id)
            if isinstance(item, Ambiguous):
                rec.lines.append(f"AMBIGUOUS: '{change.item}' could be {', '.join(item.options)}")
                continue
            if item.created:
                rec.lines.append(f"NEW: {item.name}")
            quantity = None if change.quantity is None else Decimal(str(change.quantity))
            await inventory.apply_change(
                rec, inventory.Change(item.id, change.action, quantity, change.unit, location_id, change.expires_on),
                source,
            )
            if change.price is not None:
                priced.append(spending.Line(item.id, item.name, spending.cents(change.price), quantity, change.unit))
        if source == "receipt":
            printed = args.receipt or Receipt()
            await spending.keep(
                rec, priced, shop=printed.shop, purchased_on=printed.bought_on,
                total=None if printed.total is None else spending.cents(printed.total),
                more_to_come=len(priced) < sum(1 for change in args.changes if change.price is not None))
        elif priced or args.receipt:
            rec.lines.append("NOTE: prices are kept from receipts only, so nothing was added to the spending")
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
