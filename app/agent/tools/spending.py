"""Spending tools: get_spending, update_receipt. Receipts are kept by log_inventory."""
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

from app.agent.actions import record
from app.agent.base import Ctx, ToolError
from app.agent.resolve import Ambiguous, match_item
from app.core.timeutil import local
from app.services import households, inventory, spending


class GetSpending(BaseModel):
    start: date | None = Field(None, description=(
        "First day, YYYY-MM-DD. Omit both days for this month so far; for 'last month' give its first and last day"))
    end: date | None = Field(None, description="Last day, YYYY-MM-DD")
    shop: str | None = Field(None, description="Only one shop, e.g. 'Tesco'")
    on: str | None = Field(None, description=(
        "Only one kind of thing ('food', 'household', 'toiletries', 'meat and fish', 'drinks') or one item ('milk')"))


async def get_spending(ctx: Ctx, args: GetSpending) -> str:
    """What the household spent, added up from the receipts they sent: the total, by shop and by
    kind of thing, and price changes on what they always keep in. Use it for every question about
    money spent or what something cost, and say only the amounts it returns: never add up, estimate
    or fill a gap yourself. Where it says receipts were read without prices, say that what they
    cost is not known; never say nothing was spent."""
    conn = ctx.conn
    today = local(ctx.now, await households.timezone(conn, ctx.household_id)).date()
    start, end = args.start or (args.end or today).replace(day=1), args.end or today
    if start > end:
        raise ToolError("the first day is after the last day")
    whole = await spending.summary(conn, ctx.household_id, start, end, shop=args.shop)
    if not args.on:
        lines = spending.summary_lines(whole, shop=args.shop)
        changes = await spending.staple_price_changes(conn, ctx.household_id, start, end)
        return "\n".join([*lines, *(f"Price change: {spending.change_text(change, whole.currency)}"
                                    for change in changes[:5])])
    sections, item_id, label = spending.sections_called(args.on), None, args.on
    if sections is None:
        item = await match_item(conn, ctx.household_id, args.on)
        if isinstance(item, Ambiguous):
            return f"AMBIGUOUS: '{args.on}' could be {', '.join(item.options)}"
        if item is not None:
            item_id, label = item.id, item.name
        elif section := spending.SECTION_WORDS.get(args.on.strip().lower()):
            sections = (section, (section,))
        else:
            raise ToolError(f"nothing called '{args.on}' has been bought on a receipt. The kinds are food, "
                            + ", ".join(inventory.CATEGORIES))
    found = await spending.spent_on(conn, ctx.household_id, start, end, sections[0] if sections else label,
                                    sections=sections[1] if sections else None, item_id=item_id, shop=args.shop)
    return "\n".join(spending.spent_on_lines(found, whole, today, shop=args.shop))


class LinePrice(BaseModel):
    item: str = Field(description="The item on the receipt, e.g. 'milk'")
    price: float = Field(ge=0, description="What that line really cost")


class UpdateReceipt(BaseModel):
    shop: str | None = Field(None, description="Which receipt: the shop it is from. Omit for the newest receipt")
    bought_on: date | None = Field(None, description="Which receipt: the day it is from, YYYY-MM-DD")
    total: float | None = Field(None, ge=0, description="Which receipt: its total, when two could be meant")
    new_shop: str | None = Field(None, description="The shop it is really from")
    new_date: date | None = Field(None, description="The day it is really from, YYYY-MM-DD")
    new_total: float | None = Field(None, ge=0, description="The total it really shows")
    prices: list[LinePrice] = Field([], description="Lines whose price was read wrongly")
    remove: bool = Field(False, description="True to take the whole receipt out of the spending")


async def update_receipt(ctx: Ctx, args: UpdateReceipt) -> str:
    """Correct or remove a receipt kept for the spending: 'the Tesco one was £45.10, not £54.10',
    'that receipt was from Lidl', 'the milk was 1.75', 'take that receipt out, it wasn't ours'.
    Removing changes the spending only, never the stock. Right after a receipt was read, a plain
    'undo' is undo_last instead."""
    conn = ctx.conn
    today = local(ctx.now, await households.timezone(conn, ctx.household_id)).date()
    found = await spending.find(conn, ctx.household_id, shop=args.shop, on=args.bought_on,
                                total=None if args.total is None else spending.cents(args.total))
    if not found:
        newest = await spending.find(conn, ctx.household_id, limit=3)
        raise ToolError("no receipt like that is kept. " + (
            "The newest are: " + "; ".join(spending.describe(receipt, today) for receipt in newest) if newest
            else "No receipt has been kept yet"))
    if args.bought_on and len(found) > 1:
        return "AMBIGUOUS: that could be " + "; ".join(spending.describe(receipt, today) for receipt in found)
    receipt = found[0]
    async with record(ctx, "update_receipt", args) as rec:
        if args.remove:
            await spending.remove(rec, receipt["id"])
            return rec.result
        changes = {name: value for name, value in (
            ("shop", args.new_shop), ("purchased_on", args.new_date),
            ("total", None if args.new_total is None else spending.cents(args.new_total))) if value is not None}
        prices: dict[str, Decimal | None] = {}
        for line in args.prices:
            item = await match_item(conn, ctx.household_id, line.item)
            on_it = None if item is None or isinstance(item, Ambiguous) else await spending.line_for(
                conn, receipt["id"], item.id)
            if on_it is None:
                rec.lines.append(f"ERROR: there is no line for {line.item} on that receipt")
                continue
            prices[on_it] = spending.cents(line.price)
        if not (changes or prices):
            if rec.lines:
                return rec.result
            raise ToolError("say what to change: new_shop, new_date, new_total, prices, or remove")
        await spending.correct(rec, receipt["id"], changes, prices)
    return rec.result
