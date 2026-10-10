"""Spending, from the receipts a household sends (ADR 0040).

A receipt that is read keeps its shop, its date, its printed total and the price of each line
that was logged, beside the stock log and never instead of it. Every amount a person is told is
added up here from those rows: the model reads the receipt and asks the question, code does the
sums. A receipt read before prices were kept has stock events and no row here; it is counted as
"read without prices" and never as nothing spent.
"""
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.agent.actions import Recorder
from app.agent.base import ToolError
from app.core.timeutil import local
from app.db import execute, fetch_all, fetch_one, fetch_val
from app.services import inventory, shopping

# The sections that are food and drink: what "on food" adds up.
FOOD = ("fruit and veg", "dairy and eggs", "meat and fish", "bakery", "frozen", "cupboard", "drinks")
FOOD_WORDS = ("food", "groceries", "grocery", "food shopping", "food and drink")
# One word that names a section when no item is called that.
SECTION_WORDS = {"fruit": "fruit and veg", "veg": "fruit and veg", "vegetables": "fruit and veg",
                 "dairy": "dairy and eggs", "meat": "meat and fish", "fish": "meat and fish",
                 "cleaning": "household"}
UNSORTED = "not sorted yet"       # a line whose item has no section yet: the nightly job gives it one
NO_SHOP = "a shop whose name was not read"
CENT = Decimal("0.01")
MONTHS_SHOWN = 6
# How a currency is written before an amount; any other three-letter code is written as itself.
SYMBOLS = {"GBP": "£", "EUR": "€", "USD": "$", "NGN": "₦", "GHS": "GH₵", "KES": "KSh ", "ZAR": "R", "INR": "₹",
           "CAD": "CA$", "AUD": "A$"}
CURRENCIES = {"GBP": "British pounds", "EUR": "Euros", "USD": "US dollars", "NGN": "Nigerian naira",
              "GHS": "Ghanaian cedis", "KES": "Kenyan shillings", "ZAR": "South African rand",
              "INR": "Indian rupees", "CAD": "Canadian dollars", "AUD": "Australian dollars"}
_SAID = {"£": "GBP", "pound": "GBP", "pounds": "GBP", "sterling": "GBP", "€": "EUR", "euro": "EUR", "euros": "EUR",
         "$": "USD", "dollar": "USD", "dollars": "USD", "₦": "NGN", "naira": "NGN", "cedi": "GHS", "cedis": "GHS",
         "rand": "ZAR", "rupee": "INR", "rupees": "INR"}


def cents(amount: Decimal | float | str) -> Decimal:
    return Decimal(str(amount)).quantize(CENT, rounding=ROUND_HALF_UP)


def money(amount: Decimal, currency: str) -> str:
    """£5.80, -£1.00, CHF 12.50."""
    return f"{'-' if amount < 0 else ''}{SYMBOLS.get(currency, currency + ' ')}{abs(amount):,.2f}"


def currency_code(said: str) -> str:
    """A currency as someone names it: GBP, eur, pounds, €."""
    text = said.strip()
    code = _SAID.get(text.lower()) or text.upper()
    if not re.fullmatch(r"[A-Z]{3}", code):
        raise ToolError(f"'{said}' is not a currency; give its three letters, e.g. GBP, EUR, USD or NGN")
    return code


def month_of(day: date) -> tuple[date, date]:
    """The first and last day of the month `day` is in."""
    start = day.replace(day=1)
    return start, (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)


def month_before(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def period_text(start: date, end: date) -> str:
    """'October 2026' for a whole month, else '1 Oct to 10 Oct 2026'."""
    if (start, end) == month_of(start):
        return f"{start:%B %Y}"
    if start == end:
        return f"{start:%a %-d %b %Y}"
    return f"{start:%-d %b}{'' if start.year == end.year else f' {start:%Y}'} to {end:%-d %b %Y}"


def day_text(day: date, today: date) -> str:
    return f"{day:%a %-d %b}" if day.year == today.year else f"{day:%a %-d %b %Y}"


# ---------------------------------------------------------------- the household's currency
async def currency(conn: AsyncConnection, household_id: str) -> str:
    return str(await fetch_val(conn, "select currency from households where id = :h", h=household_id))


async def set_currency(rec: Recorder, said: str) -> None:
    """What the household's amounts are in from now on. Receipts already kept stay in the
    currency they were read in, and are left out of sums in another."""
    code = currency_code(said)
    await rec.before("households", id=rec.ctx.household_id)
    await execute(rec.ctx.conn, "update households set currency = :code where id = :h", code=code,
                  h=rec.ctx.household_id)
    rec.lines.append(f"OK: amounts are in {CURRENCIES.get(code, code)} ({money(Decimal('1.50'), code)})")


# ---------------------------------------------------------------- keeping a receipt
@dataclass(frozen=True)
class Line:
    """One line of a receipt that was logged to stock and has a printed price."""
    item_id: str
    label: str
    price: Decimal
    quantity: Decimal | None = None
    unit: str | None = None


async def shop_name(conn: AsyncConnection, household_id: str, printed: str | None) -> str | None:
    """The shop as this household already calls it, so "TESCO STORES 2041" adds to their Tesco:
    a shop they named, or one on an earlier receipt, that reads like the printed name. Otherwise
    the printed name, without the capitals receipts shout in."""
    name = " ".join((printed or "").split())
    if not name:
        return None
    known = await fetch_val(
        conn,
        """select name from (select name from places where household_id = :h and kind = 'store'
                             union select shop from receipts
                                   where household_id = :h and status = 'kept' and shop is not null) known
           where greatest(word_similarity(name, :printed), word_similarity(:printed, name)) >= :alike
           order by greatest(word_similarity(name, :printed), word_similarity(:printed, name)) desc,
                    similarity(name, :printed) desc, name limit 1""",
        h=household_id, printed=name, alike=shopping.SAME_SHOP)
    return str(known) if known else (name.title() if name.isupper() else name)


async def started(conn: AsyncConnection, household_id: str, message_id: str | None) -> bool:
    """Whether a receipt has been kept from this message already, by an earlier call of its turn."""
    return message_id is not None and bool(await fetch_val(
        conn, "select exists (select 1 from receipts where household_id = :h and source_message_id = :message "
              "and status = 'kept')", h=household_id, message=message_id))


async def keep(rec: Recorder, lines: list[Line], *, shop: str | None = None, purchased_on: date | None = None,
               total: Decimal | None = None, more_to_come: bool = False) -> None:
    """Keep what a receipt cost, in the action that logged its stock, so undo takes both back.

    Nothing is made up: a receipt with no readable total keeps none, a line without a price keeps
    no line, and a receipt with neither is not kept at all. The model may log one receipt in two
    calls (a name it had to correct, so `more_to_come`): the second adds to the first. The same
    receipt sent again on another day is not counted twice."""
    ctx, conn = rec.ctx, rec.ctx.conn
    household = await fetch_one(conn, "select timezone, currency from households where id = :h", h=ctx.household_id)
    assert household is not None
    today = local(ctx.now, household["timezone"]).date()
    shop = await shop_name(conn, ctx.household_id, shop)
    printed_day = purchased_on if purchased_on is not None and purchased_on <= today else None

    started = None if ctx.message_id is None else await fetch_one(
        conn,
        """select id, shop, purchased_on, total, currency from receipts
           where household_id = :h and source_message_id = :message and status = 'kept'
             and (cast(:shop as text) is null or shop is null or lower(shop) = lower(:shop))
             and (cast(:day as date) is null or purchased_on = :day)
           order by created_at desc limit 1 for update""",
        h=ctx.household_id, message=ctx.message_id, shop=shop, day=printed_day)
    if started:
        await _add_to(rec, started, lines, shop, total, today)
        return
    if total is None and not lines:
        if not more_to_come:
            rec.lines.append("NOTE: no price could be read on this receipt, so it is not in the spending")
        return
    same = None if not (printed_day and shop and total is not None) else await fetch_one(
        conn,
        """select id, shop, purchased_on, total, currency from receipts
           where household_id = :h and status = 'kept' and lower(shop) = lower(:shop)
             and purchased_on = :day and total = :total limit 1""",
        h=ctx.household_id, shop=shop, day=printed_day, total=total)

    receipt = await fetch_one(
        conn,
        """insert into receipts (household_id, shop, purchased_on, total, currency, status, member_id,
                                 source_message_id, created_at)
           values (:h, :shop, :day, :total, :currency, :status, :member, :message, clock_timestamp())
           returning id, shop, purchased_on, total, currency""",
        h=ctx.household_id, shop=shop, day=printed_day or today, total=total, currency=household["currency"],
        status="removed" if same else "kept", member=ctx.member_id, message=ctx.message_id)
    assert receipt is not None
    rec.created("receipts", receipt["id"])
    if same:
        # Kept as a row that counts for nothing, so this message is not taken for a receipt read without prices.
        rec.lines.append(f"NOTE: this receipt was already kept ({describe(same, today)}), so it is not counted "
                         "twice in the spending")
        return
    await _add_lines(rec, receipt["id"], lines, 0)
    listed = sum((line.price for line in lines), Decimal(0))
    prices = f"{len(lines)} line price{'' if len(lines) == 1 else 's'}"
    rec.lines.append(
        f"OK: receipt kept for the spending: {describe(receipt, today)}, {prices}" if total is not None else
        f"OK: receipt kept for the spending: {describe({**receipt, 'total': listed}, today)} from {prices}; "
        "no total could be read on it")
    if printed_day is None:
        rec.lines.append("NOTE: no date on the receipt could be used, so it counts as bought today")


async def _add_to(rec: Recorder, receipt: dict[str, Any], lines: list[Line], shop: str | None,
                  total: Decimal | None, today: date) -> None:
    """More of a receipt this message already started."""
    conn = rec.ctx.conn
    have = await fetch_all(conn, "select item_id, price from receipt_lines where receipt_id = :id", id=receipt["id"])
    seen = {(line["item_id"], line["price"]) for line in have}
    fresh = [line for line in lines if (line.item_id, line.price) not in seen]
    fills = (receipt["shop"] is None and shop is not None) or (receipt["total"] is None and total is not None)
    if not (fresh or fills):
        return
    await rec.before("receipts", id=receipt["id"])
    after = await fetch_one(
        conn, "update receipts set shop = coalesce(shop, :shop), total = coalesce(total, :total) where id = :id "
              "returning id, shop, purchased_on, total, currency", shop=shop, total=total, id=receipt["id"])
    assert after is not None
    await _add_lines(rec, receipt["id"], fresh, len(have))
    rec.lines.append(f"OK: {len(fresh)} more line price{'' if len(fresh) == 1 else 's'} kept on the receipt "
                     f"({describe(after, today)})")


async def _add_lines(rec: Recorder, receipt_id: str, lines: list[Line], after: int) -> None:
    for position, line in enumerate(lines, start=after):
        line_id = await fetch_val(
            rec.ctx.conn,
            """insert into receipt_lines (receipt_id, item_id, label, quantity, unit, price, position)
               values (:receipt, :item, :label, :quantity, :unit, :price, :position) returning id""",
            receipt=receipt_id, item=line.item_id, label=line.label, quantity=line.quantity, unit=line.unit,
            price=line.price, position=position)
        rec.created("receipt_lines", str(line_id))


def describe(receipt: dict[str, Any], today: date) -> str:
    """'Freshway Supermarket, Tue 6 Oct, £5.80'."""
    parts = [receipt["shop"] or NO_SHOP, day_text(receipt["purchased_on"], today)]
    if receipt["total"] is not None:
        parts.append(money(receipt["total"], receipt["currency"]))
    return ", ".join(parts)


# ---------------------------------------------------------------- correcting and removing
_RECEIPT = """select r.id, r.shop, r.purchased_on, r.total, r.currency, r.created_at,
                     coalesce((select sum(l.price) from receipt_lines l where l.receipt_id = r.id), 0) as listed,
                     (select count(*) from receipt_lines l where l.receipt_id = r.id) as lines
              from receipts r where r.household_id = :h and r.status = 'kept'"""


async def find(conn: AsyncConnection, household_id: str, *, shop: str | None = None, on: date | None = None,
               total: Decimal | None = None, limit: int = 5) -> list[dict[str, Any]]:
    """Kept receipts that fit what was said about one, newest first."""
    return await fetch_all(
        conn,
        _RECEIPT + """ and (cast(:shop as text) is null or (r.shop is not null and
                 greatest(word_similarity(r.shop, :shop), word_similarity(:shop, r.shop)) >= :alike))
             and (cast(:day as date) is null or r.purchased_on = :day)
             and (cast(:total as numeric) is null or r.total = :total)
           order by r.purchased_on desc, r.created_at desc limit :limit""",
        h=household_id, shop=shop, day=on, total=total, alike=shopping.SAME_SHOP, limit=limit)


async def line_for(conn: AsyncConnection, receipt_id: str, item_id: str) -> str | None:
    """The receipt's line for an item, if it has one."""
    found = await fetch_val(conn, "select id from receipt_lines where receipt_id = :receipt and item_id = :item "
                                  "order by position limit 1", receipt=receipt_id, item=item_id)
    return None if found is None else str(found)


async def _kept(rec: Recorder, receipt_id: str) -> tuple[dict[str, Any], date]:
    """The receipt, locked, and today's date in the household."""
    receipt = await fetch_one(rec.ctx.conn, _RECEIPT + " and r.id = :id for update of r", h=rec.ctx.household_id,
                              id=receipt_id)
    if receipt is None:
        raise ToolError("that receipt is no longer kept")
    timezone = await fetch_val(rec.ctx.conn, "select timezone from households where id = :h", h=rec.ctx.household_id)
    return receipt, local(rec.ctx.now, timezone).date()


async def correct(rec: Recorder, receipt_id: str, changes: dict[str, Any],
                  prices: dict[str, Decimal | None] | None = None) -> None:
    """Put right what was read. `changes` holds the fields to set, of `shop`, `purchased_on` and
    `total` (None: there is no total, the lines are added up). `prices` gives lines a new price
    by line id, or None to drop a line that should have no price."""
    receipt, today = await _kept(rec, receipt_id)
    conn = rec.ctx.conn
    fields = {name: value for name, value in changes.items() if name in ("shop", "purchased_on", "total")}
    if "purchased_on" in fields and (fields["purchased_on"] is None or fields["purchased_on"] > today):
        raise ToolError("a receipt's date is today or earlier")
    if "shop" in fields:
        fields["shop"] = " ".join((fields["shop"] or "").split()) or None
    if fields.get("total") is not None and fields["total"] < 0:
        raise ToolError("a total cannot be below zero")
    await rec.before("receipts", id=receipt_id)
    if fields:
        await execute(conn, f"update receipts set {', '.join(f'{name} = :{name}' for name in fields)} where id = :id",
                      id=receipt_id, **fields)
    for line_id, price in (prices or {}).items():
        if not await fetch_val(conn, "select exists (select 1 from receipt_lines where id = :id and "
                                     "receipt_id = :receipt)", id=line_id, receipt=receipt_id):
            raise ToolError("that line is not on this receipt")
        if price is not None and price < 0:
            raise ToolError("a price cannot be below zero")
        await rec.before("receipt_lines", id=line_id)
        if price is None:
            await execute(conn, "delete from receipt_lines where id = :id", id=line_id)
        else:
            await execute(conn, "update receipt_lines set price = :price where id = :id", price=price, id=line_id)
    after, _ = await _kept(rec, receipt_id)
    amount = after["total"] if after["total"] is not None else after["listed"]
    rec.lines.append(f"OK: receipt corrected: {describe({**after, 'total': amount}, today)}"
                     + ("" if after["total"] is not None else ", added up from its lines"))


async def remove(rec: Recorder, receipt_id: str) -> None:
    """Take a receipt out of the spending. Its stock events are another matter and stay."""
    receipt, today = await _kept(rec, receipt_id)
    await rec.before("receipts", id=receipt_id)
    await execute(rec.ctx.conn, "update receipts set status = 'removed' where id = :id", id=receipt_id)
    amount = receipt["total"] if receipt["total"] is not None else receipt["listed"]
    rec.lines.append(f"OK: receipt removed from the spending: {describe({**receipt, 'total': amount}, today)}")
    rec.lines.append("NOTE: what it added to the stock is unchanged")


# ---------------------------------------------------------------- adding up
@dataclass(frozen=True)
class Summary:
    start: date
    end: date
    currency: str
    total: Decimal
    receipts: int
    shops: list[tuple[str, Decimal]]     # most spent first
    kinds: list[tuple[str, Decimal]]     # line prices by supermarket section, most first
    unlisted: Decimal                    # paid, by the totals, for lines that have no price kept
    discounts: Decimal                   # taken off at the till: how far totals fall below their lines; zero or less
    unpriced: int                        # receipts read in this time with no prices kept
    elsewhere: int                       # receipts kept in another currency, not added in


# Kept receipts of one household, in its currency, bought between two days, at one shop if one is named.
_IN_TIME = """r.household_id = :h and r.status = 'kept' and r.purchased_on between :start and :end
  and (cast(:shop as text) is null or (r.shop is not null and
       greatest(word_similarity(r.shop, :shop), word_similarity(:shop, r.shop)) >= :alike))"""


async def unpriced(conn: AsyncConnection, household_id: str, start: date, end: date) -> int:
    """Receipts read in this time that have no prices kept: stock events from a receipt whose
    message has no receipt row. Every receipt read before this was built is one."""
    return int(await fetch_val(
        conn,
        f"""select count(distinct coalesce(e.source_message_id, e.id)) from {inventory.LIVE_EVENTS}
              and e.household_id = :h and e.source = 'receipt'
              and (e.occurred_at at time zone (select timezone from households where id = :h))::date
                  between :start and :end
              and not exists (select 1 from receipts r where r.household_id = e.household_id
                              and r.source_message_id = e.source_message_id)""",
        h=household_id, start=start, end=end))


async def summary(conn: AsyncConnection, household_id: str, start: date, end: date, *,
                  shop: str | None = None) -> Summary:
    """What was spent between two days, inclusive. A receipt counts for its printed total, or
    for its lines added up when it shows none."""
    money_in = await currency(conn, household_id)
    params = {"h": household_id, "start": start, "end": end, "shop": shop, "alike": shopping.SAME_SHOP}
    receipts = await fetch_all(
        conn,
        f"""select coalesce(r.shop, :no_shop) as shop, r.total, coalesce(sum(l.price), 0) as listed
            from receipts r left join receipt_lines l on l.receipt_id = r.id
            where {_IN_TIME} and r.currency = :currency group by r.id""",
        **params, currency=money_in, no_shop=NO_SHOP)
    kinds = await fetch_all(
        conn,
        f"""select coalesce(i.category, :unsorted) as kind, sum(l.price) as spent
            from receipt_lines l join receipts r on r.id = l.receipt_id left join items i on i.id = l.item_id
            where {_IN_TIME} and r.currency = :currency group by 1 order by 2 desc, 1""",
        **params, currency=money_in, unsorted=UNSORTED)
    elsewhere = await fetch_val(conn, f"select count(*) from receipts r where {_IN_TIME} and r.currency <> :currency",
                                **params, currency=money_in)
    shops: dict[str, Decimal] = {}
    unlisted = discounts = Decimal(0)
    for receipt in receipts:
        amount = receipt["total"] if receipt["total"] is not None else receipt["listed"]
        shops[receipt["shop"]] = shops.get(receipt["shop"], Decimal(0)) + amount
        gap = amount - receipt["listed"]
        unlisted, discounts = unlisted + max(gap, Decimal(0)), discounts + min(gap, Decimal(0))
    return Summary(
        start=start, end=end, currency=money_in, total=sum(shops.values(), Decimal(0)), receipts=len(receipts),
        shops=sorted(shops.items(), key=lambda entry: (-entry[1], entry[0])),
        kinds=[(row["kind"], row["spent"]) for row in kinds], unlisted=unlisted, discounts=discounts,
        unpriced=await unpriced(conn, household_id, start, end), elsewhere=int(elsewhere))


@dataclass(frozen=True)
class Month:
    start: date
    total: Decimal
    receipts: int
    unpriced: int


async def months(conn: AsyncConnection, household_id: str, upto: date, count: int = MONTHS_SHOWN) -> list[Month]:
    """The month `upto` is in and the ones before it, oldest first."""
    found: list[Month] = []
    start = upto.replace(day=1)
    for _ in range(count):
        one = await summary(conn, household_id, *month_of(start))
        found.append(Month(start, one.total, one.receipts, one.unpriced))
        start = month_before(start)
    return found[::-1]


async def receipts_between(conn: AsyncConnection, household_id: str, start: date, end: date) -> list[dict[str, Any]]:
    """Kept receipts with their lines, newest first: what the Spending page lists and corrects."""
    receipts = await fetch_all(
        conn, _RECEIPT + " and r.purchased_on between :start and :end order by r.purchased_on desc, r.created_at desc",
        h=household_id, start=start, end=end)
    lines = await fetch_all(
        conn,
        """select l.id, l.receipt_id, l.label, l.quantity, l.unit, l.price from receipt_lines l
           where l.receipt_id = any(cast(:ids as uuid[])) order by l.position, l.id""",
        ids=[receipt["id"] for receipt in receipts])
    for receipt in receipts:
        receipt["amount"] = receipt["total"] if receipt["total"] is not None else receipt["listed"]
        receipt["items"] = [line for line in lines if line["receipt_id"] == receipt["id"]]
    return receipts


# ---------------------------------------------------------------- one kind of thing, or one item
@dataclass(frozen=True)
class SpentOn:
    what: str                            # "food", a section, or an item's name
    amount: Decimal
    receipts: int
    unsorted: Decimal                    # line prices in this time whose item has no section yet (kinds only)
    paid: list[dict[str, Any]]           # an item's newest prices: price, shop, purchased_on


def sections_called(what: str) -> tuple[str, tuple[str, ...]] | None:
    """The supermarket sections a word names outright: 'food' is seven of them."""
    said = " ".join(what.lower().split())
    if said in FOOD_WORDS:
        return "food", FOOD
    return (said, (said,)) if said in inventory.CATEGORIES else None


async def spent_on(conn: AsyncConnection, household_id: str, start: date, end: date, label: str, *,
                   sections: tuple[str, ...] | None = None, item_id: str | None = None,
                   shop: str | None = None) -> SpentOn:
    """What the lines for some sections, or for one item, add up to between two days."""
    params = {"h": household_id, "start": start, "end": end, "shop": shop, "alike": shopping.SAME_SHOP,
              "currency": await currency(conn, household_id)}
    found = await fetch_one(
        conn,
        f"""select coalesce(sum(l.price) filter (where i.id = :item or i.category = any(cast(:sections as text[]))), 0)
                     as amount,
                   count(distinct r.id) filter (where i.id = :item or i.category = any(cast(:sections as text[])))
                     as receipts,
                   coalesce(sum(l.price) filter (where i.category is null), 0) as unsorted
            from receipt_lines l join receipts r on r.id = l.receipt_id left join items i on i.id = l.item_id
            where {_IN_TIME} and r.currency = :currency""",
        **params, item=item_id, sections=list(sections or ()))
    assert found is not None
    paid = [] if item_id is None else await fetch_all(
        conn,
        """select l.price, l.quantity, l.unit, r.shop, r.purchased_on
           from receipt_lines l join receipts r on r.id = l.receipt_id
           where r.household_id = :h and r.status = 'kept' and r.currency = :currency and l.item_id = :item
             and r.purchased_on <= :end
           order by r.purchased_on desc, r.created_at desc limit 3""",
        h=household_id, currency=params["currency"], item=item_id, end=end)
    return SpentOn(label, found["amount"], found["receipts"], found["unsorted"] if sections else Decimal(0), paid)


# ---------------------------------------------------------------- price changes on staples
@dataclass(frozen=True)
class PriceChange:
    item: str
    shop: str
    now: Decimal
    was: Decimal
    per: str                             # "" for the same pack, else what one of it is: "kg", "each"
    on: date

    @property
    def percent(self) -> Decimal:
        return (self.now - self.was) / self.was * 100 if self.was else Decimal(0)


def _unit(unit: str | None) -> str:
    text = (unit or "").strip().lower()
    return text[:-1] if text.endswith("s") and not text.endswith("ss") else text


def _comparison(now: dict[str, Any], was: dict[str, Any]) -> tuple[str, Decimal, Decimal] | None:
    """How two purchases of one item compare: the same pack by what it cost; two sizes in the
    same unit by what one of that unit cost. None when there is no honest comparison."""
    if _unit(now["unit"]) != _unit(was["unit"]) or (now["quantity"] is None) != (was["quantity"] is None):
        return None
    if now["quantity"] == was["quantity"]:
        return "", now["price"], was["price"]
    if not (now["quantity"] and was["quantity"]):
        return None
    return _unit(now["unit"]) or "each", cents(now["price"] / now["quantity"]), cents(was["price"] / was["quantity"])


def price_changes(lines: list[dict[str, Any]], start: date, end: date) -> list[PriceChange]:
    """For each item bought between two days: its newest price against the last one before it at
    the same shop, where the two can be compared. `lines` are one household's priced lines, oldest
    first. Biggest change first; an unchanged price is not a change."""
    by_item: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
        if line["purchased_on"] <= end:
            by_item.setdefault(line["item_id"], []).append(line)
    changes: list[PriceChange] = []
    for bought in by_item.values():
        newest = bought[-1]
        if newest["purchased_on"] < start:
            continue
        for earlier in reversed(bought[:-1]):
            if earlier["receipt_id"] == newest["receipt_id"] or earlier["shop"].lower() != newest["shop"].lower():
                continue
            compared = _comparison(newest, earlier)
            if compared is None:
                continue
            per, now, was = compared
            if now != was:
                changes.append(PriceChange(newest["item"], newest["shop"], now, was, per, newest["purchased_on"]))
            break
    return sorted(changes, key=lambda change: (-abs(change.percent), change.item))


async def staple_price_changes(conn: AsyncConnection, household_id: str, start: date, end: date) -> list[PriceChange]:
    """Price changes on what the household always keeps in, for purchases between two days."""
    lines = await fetch_all(
        conn,
        """select l.item_id, i.canonical_name as item, l.price, l.quantity, l.unit, l.receipt_id, r.shop,
                  r.purchased_on
           from receipt_lines l join receipts r on r.id = l.receipt_id join items i on i.id = l.item_id
           where r.household_id = :h and r.status = 'kept' and r.shop is not null and i.is_staple
             and r.currency = (select currency from households where id = :h) and r.purchased_on <= :end
           order by r.purchased_on, r.created_at, l.position""",
        h=household_id, end=end)
    return price_changes(lines, start, end)


def change_text(change: PriceChange, currency: str) -> str:
    """'milk at Freshway Supermarket: £1.75, was £1.65 (up £0.10)'."""
    per = f" a {change.per}" if change.per and change.per != "each" else (" each" if change.per else "")
    moved = "up" if change.now > change.was else "down"
    return (f"{change.item} at {change.shop}: {money(change.now, currency)}{per}, was "
            f"{money(change.was, currency)}{per} ({moved} {money(abs(change.now - change.was), currency)})")


# ---------------------------------------------------------------- said in words
def _count(number: int, noun: str = "receipt") -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def kind_parts(found: Summary) -> list[str]:
    """'meat and fish £30.10', ..., then what the lines do not account for."""
    parts = [f"{kind} {money(spent, found.currency)}" for kind, spent in found.kinds]
    if found.unlisted:
        parts.append(f"not listed line by line {money(found.unlisted, found.currency)}")
    if found.discounts:
        parts.append(f"discounts {money(found.discounts, found.currency)}")
    return parts


def gaps(found: Summary) -> list[str]:
    """What the total does not hold, said so the answer cannot pass it off as nothing spent."""
    notes = []
    if found.unpriced and found.receipts:
        notes.append(f"NOTE: {_count(found.unpriced, 'more receipt')} read in this time "
                     f"{'has' if found.unpriced == 1 else 'have'} no prices kept: what "
                     f"{'it' if found.unpriced == 1 else 'they'} cost is not known and is not in the total")
    elif found.unpriced:
        notes.append(f"NOTE: {_count(found.unpriced)} {'was' if found.unpriced == 1 else 'were'} read in this "
                     "time without prices being kept, so what was spent is not known. Say that; do not say "
                     "nothing was spent")
    if found.elsewhere:
        notes.append(f"NOTE: {_count(found.elsewhere)} in another currency {'is' if found.elsewhere == 1 else 'are'} "
                     "not added in")
    return notes


def summary_lines(found: Summary, *, shop: str | None = None) -> list[str]:
    """A summary as tool result lines."""
    where, when = f" at {shop}" if shop else "", period_text(found.start, found.end)
    if not found.receipts:
        return [f"OK: no receipt with prices{where} for {when}", *gaps(found)]
    lines = [f"OK: spent{where}, {when}: {money(found.total, found.currency)} on {_count(found.receipts)}"]
    if not shop:
        lines.append("By shop: " + "; ".join(f"{name} {money(spent, found.currency)}" for name, spent in found.shops))
    if parts := kind_parts(found):
        lines.append("By kind: " + "; ".join(parts))
    return [*lines, *gaps(found)]


def spent_on_lines(found: SpentOn, whole: Summary, today: date, *, shop: str | None = None) -> list[str]:
    where, when = f" at {shop}" if shop else "", period_text(whole.start, whole.end)
    lines = [f"OK: spent on {found.what}{where}, {when}: {money(found.amount, whole.currency)} on "
             f"{_count(found.receipts)}, from the price of each line"]
    if found.paid:
        lines.append("Last paid: " + "; ".join(
            f"{money(paid['price'], whole.currency)} at {paid['shop'] or NO_SHOP} on "
            f"{day_text(paid['purchased_on'], today)}" for paid in found.paid))
    if found.unsorted:
        lines.append(f"NOTE: {money(found.unsorted, whole.currency)} more is on lines not sorted into a kind yet")
    return [*lines, *gaps(whole)]


async def month_report(conn: AsyncConnection, household_id: str, month: date) -> str | None:
    """A month's spending as a few lines for the weekly digest. None when the month has no
    receipt with prices: a month of receipts read before prices were kept is not news."""
    start, end = month_of(month)
    found = await summary(conn, household_id, start, end)
    if not found.receipts:
        return None
    lines = [f"Spending in {start:%B}: {money(found.total, found.currency)} from {_count(found.receipts)}."]
    shown, rest = found.shops[:3], found.shops[3:]
    lines += [f"- {name} {money(spent, found.currency)}" for name, spent in shown]
    if rest:
        lines.append(f"- {_count(len(rest), 'other shop')} "
                     f"{money(sum((spent for _, spent in rest), Decimal(0)), found.currency)}")
    kinds = [f"{kind} ({money(spent, found.currency)})" for kind, spent in found.kinds if kind != UNSORTED][:3]
    if kinds:
        lines.append("Most went on " + (", ".join(kinds[:-1]) + " and " + kinds[-1] if len(kinds) > 1 else kinds[0])
                     + ".")
    before = await summary(conn, household_id, *month_of(month_before(start)))
    if before.receipts:
        lines.append(f"{before.start:%B} was {money(before.total, before.currency)}.")
    if found.unpriced:
        lines.append(f"{_count(found.unpriced, 'more receipt')} had no prices kept, so the real figure is higher.")
    changes = await staple_price_changes(conn, household_id, start, end)
    lines += [f"Price change: {change_text(change, found.currency)}." for change in changes[:3]]
    return "\n".join(lines)
