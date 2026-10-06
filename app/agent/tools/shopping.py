"""Shopping list tools: update_shopping_list, get_shopping_list."""
from decimal import Decimal

from pydantic import BaseModel, Field

from app.agent.actions import record
from app.agent.base import Ctx
from app.agent.resolve import Ambiguous, match_item, resolve_item
from app.services import shopping


class ShoppingAdd(BaseModel):
    item: str
    quantity: float | None = None
    unit: str | None = None
    store_hint: str | None = Field(None, description="e.g. 'African shop', 'Costco'")


class UpdateShoppingList(BaseModel):
    add: list[ShoppingAdd] = []
    bought: list[str] = Field([], description=(
        "Ticks off AND logs a restock to inventory. Not for anything log_inventory recorded as bought, "
        "from a receipt or a message: that is ticked off already"))
    remove: list[str] = Field([], description="No longer needed (dismissed)")
    bought_all: bool = Field(False, description="True for 'got everything on the list'")


async def update_shopping_list(ctx: Ctx, args: UpdateShoppingList) -> str:
    """Add, tick off, or remove items on the shared shopping list."""
    async with record(ctx, "update_shopping_list", args) as rec:
        for entry in args.add:
            item = await resolve_item(ctx.conn, ctx.household_id, entry.item)
            if isinstance(item, Ambiguous):
                rec.lines.append(f"AMBIGUOUS: '{entry.item}' could be {', '.join(item.options)}")
                continue
            await shopping.add(
                rec, item.id, item.name,
                quantity=None if entry.quantity is None else Decimal(str(entry.quantity)),
                unit=entry.unit, store_hint=entry.store_hint,
            )
        if args.bought_all:
            await shopping.bought_all(rec)
        for name in args.bought:
            item = await resolve_item(ctx.conn, ctx.household_id, name)
            if isinstance(item, Ambiguous):
                rec.lines.append(f"AMBIGUOUS: '{name}' could be {', '.join(item.options)}")
                continue
            await shopping.bought(rec, item.id)
        for name in args.remove:
            found = await match_item(ctx.conn, ctx.household_id, name)
            if isinstance(found, Ambiguous):
                rec.lines.append(f"AMBIGUOUS: '{name}' could be {', '.join(found.options)}")
            elif found is None:
                rec.lines.append(f"ERROR: {name} is not on the shopping list")
            else:
                await shopping.remove(rec, found.id, found.name)
    return rec.result or "ERROR: nothing to do: give add, bought, remove or bought_all"


class GetShoppingList(BaseModel):
    store: str | None = Field(None, description="Filter to one shop")
    include_predicted: bool = Field(True, description="Include items probably running low")


async def get_shopping_list(ctx: Ctx, args: GetShoppingList) -> str:
    """The current list grouped by category; explicit items first, predicted marked (probably)."""
    entries = await shopping.active_entries(
        ctx.conn, ctx.household_id, store=args.store, include_predicted=args.include_predicted
    )
    return shopping.list_text(entries)
