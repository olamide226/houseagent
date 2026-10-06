"""Shopping list write path, shared by agent tools and the dashboard."""
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.agent.actions import Recorder
from app.agent.base import ToolError
from app.db import execute, fetch_all, fetch_one, fetch_val
from app.services import inventory

SAME_SHOP = 0.6   # word similarity from which a shop named on an entry is the shop being asked about


async def add_entry(rec: Recorder, item_id: str, reason: str, *, quantity: Decimal | None = None,
                    unit: str | None = None, store_hint: str | None = None) -> bool:
    """Put an item on the list. False if it was already there (one active row per item)."""
    ctx = rec.ctx
    entry_id = await fetch_val(
        ctx.conn,
        """insert into shopping_list_items
             (household_id, item_id, quantity, unit, reason, store_hint, added_by, added_at)
           values (:h, :item, :quantity, :unit, :reason, :hint, :member, clock_timestamp())
           on conflict (household_id, item_id) where status = 'needed' and item_id is not null do nothing
           returning id""",
        h=ctx.household_id, item=item_id, quantity=quantity, unit=unit, reason=reason, hint=store_hint,
        member=ctx.member_id,
    )
    if entry_id is not None:
        rec.created("shopping_list_items", str(entry_id))
        return True
    # It is there already. A guess ("probably") becomes a real entry; anything else stays as it was.
    guess = None if reason == "predicted" else await fetch_val(
        ctx.conn, "select id from shopping_list_items where household_id = :h and item_id = :item "
                  "and status = 'needed' and reason = 'predicted' for update", h=ctx.household_id, item=item_id)
    if guess is None:
        return False
    await rec.before("shopping_list_items", id=str(guess))
    await execute(
        ctx.conn,
        """update shopping_list_items set reason = :reason, quantity = :quantity, unit = :unit, store_hint = :hint,
                                          added_by = :member, added_at = clock_timestamp() where id = :id""",
        reason=reason, quantity=quantity, unit=unit, hint=store_hint, member=ctx.member_id, id=guess)
    return True


async def on_list(conn: AsyncConnection, household_id: str, item_id: str) -> bool:
    return bool(await fetch_val(
        conn, "select exists (select 1 from shopping_list_items where household_id = :h and item_id = :item "
              "and status = 'needed')", h=household_id, item=item_id))


async def resolve_entry(rec: Recorder, item_id: str, status: str) -> bool:
    """Mark the item's active entry bought or dismissed. False if it was not on the list."""
    entry = await fetch_one(
        rec.ctx.conn,
        "select id from shopping_list_items where household_id = :h and item_id = :item and status = 'needed'",
        h=rec.ctx.household_id, item=item_id,
    )
    if entry is None:
        return False
    await rec.before("shopping_list_items", id=entry["id"])
    await execute(
        rec.ctx.conn,
        "update shopping_list_items set status = :status, resolved_at = clock_timestamp() where id = :id",
        status=status, id=entry["id"],
    )
    return True


async def add(rec: Recorder, item_id: str, name: str, *, quantity: Decimal | None = None,
              unit: str | None = None, store_hint: str | None = None) -> None:
    if await add_entry(rec, item_id, "explicit", quantity=quantity, unit=unit, store_hint=store_hint):
        rec.lines.append(f"OK: {name} added to shopping list")
    else:
        rec.lines.append(f"OK: {name} is already on the shopping list")


async def bought(rec: Recorder, item_id: str) -> None:
    """Ticking an item off is a restock; the restock rule resolves the list entry."""
    ctx = rec.ctx
    # Models often log the purchase and then tick the list too. One purchase is one restock.
    already = ctx.message_id and await fetch_val(
        ctx.conn,
        """select i.canonical_name from inventory_events e join items i on i.id = e.item_id
           where e.item_id = :item and e.source_message_id = :message
             and e.event_type in ('added', 'restocked') limit 1""",
        item=item_id, message=ctx.message_id,
    )
    if already:
        rec.lines.append(f"OK: {already} was already recorded as bought in this turn")
        return
    await inventory.apply_change(rec, inventory.Change(item_id, "restocked"), "shopping")


async def remove(rec: Recorder, item_id: str, name: str) -> None:
    if await resolve_entry(rec, item_id, "dismissed"):
        rec.lines.append(f"OK: {name} removed from shopping list")
    else:
        rec.lines.append(f"ERROR: {name} is not on the shopping list")


async def bought_all(rec: Recorder) -> None:
    # "Got everything" is about what was asked for, not what was only guessed to be running low.
    entries = await active_entries(rec.ctx.conn, rec.ctx.household_id, include_predicted=False)
    if not entries:
        raise ToolError("the shopping list is empty")
    for entry in entries:
        await bought(rec, entry["item_id"])


async def set_store_hint(rec: Recorder, entry_id: str, store_hint: str | None) -> None:
    entry = await fetch_one(
        rec.ctx.conn,
        "select id from shopping_list_items where id = :id and household_id = :h and status = 'needed'",
        id=entry_id, h=rec.ctx.household_id,
    )
    if entry is None:
        raise ToolError("that entry is no longer on the list")
    await rec.before("shopping_list_items", id=entry_id)
    await execute(rec.ctx.conn, "update shopping_list_items set store_hint = :hint where id = :id",
                  hint=store_hint or None, id=entry_id)
    rec.lines.append("OK: store updated")


# ---------------------------------------------------------------- reads
async def active_entries(conn: AsyncConnection, household_id: str, *, store: str | None = None,
                         include_predicted: bool = True) -> list[dict[str, Any]]:
    """Explicit, finished and low entries first, then predicted; grouped by category. `store`
    keeps entries with no shop named and those whose shop reads like it ("Tesco" for "Tesco Extra")."""
    return await fetch_all(
        conn,
        """select s.id, s.item_id, i.canonical_name as item, coalesce(i.category, 'other') as category,
                  s.quantity, s.unit, s.reason, s.store_hint, s.added_at
           from shopping_list_items s join items i on i.id = s.item_id
           where s.household_id = :h and s.status = 'needed'
             and (:predicted or s.reason <> 'predicted')
             and (cast(:store as text) is null or s.store_hint is null
                  or greatest(word_similarity(s.store_hint, :store), word_similarity(:store, s.store_hint)) >= :alike)
           order by (s.reason = 'predicted'), category, i.canonical_name""",
        h=household_id, predicted=include_predicted, store=store, alike=SAME_SHOP,
    )


def list_text(entries: list[dict[str, Any]]) -> str:
    if not entries:
        return "The shopping list is empty."
    lines: list[str] = []
    category = None
    for entry in entries:
        if entry["category"] != category and len({e["category"] for e in entries}) > 1:
            category = entry["category"]
            lines.append(f"{category}:")
        amount = inventory.quantity_text(entry["quantity"], entry["unit"])
        line = f"- {entry['item']}"
        if amount:
            line += f" x {amount}"
        if entry["store_hint"]:
            line += f" [{entry['store_hint']}]"
        if entry["reason"] == "predicted":
            line += " (probably)"
        lines.append(line)
    return "\n".join(lines)
