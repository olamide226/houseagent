"""Pure stock transition function (spec section 4). No I/O: the inventory service calls it
inside the transaction that appends the event, and the rebuild replays events through it."""
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

ZERO = Decimal(0)


@dataclass(frozen=True)
class StockRow:
    qty: Decimal | None = None
    status: str = "unknown"
    expires_on: date | None = None


@dataclass(frozen=True)
class StockEvent:
    type: str
    quantity: Decimal | None = None
    expires_on: date | None = None


@dataclass(frozen=True)
class Item:
    low_threshold: Decimal | None = None


def _recompute(qty: Decimal | None, previous: str, item: Item) -> str:
    if qty is None:
        return previous
    if qty == ZERO:
        return "out"
    if item.low_threshold is not None and qty <= item.low_threshold:
        return "low"
    return "in_stock"


def apply(row: StockRow | None, event: StockEvent, item: Item) -> StockRow:
    """The stock row after `event`. `row` is None when the item was never seen at this location."""
    row = row or StockRow()
    qty, status, quantity = row.qty, row.status, event.quantity
    kind = "finished" if event.type == "discarded" and quantity is None else event.type

    if kind in ("added", "restocked"):
        if qty is not None and quantity is not None:
            qty += quantity
        status = "in_stock"
    elif kind in ("used", "discarded"):
        if qty is not None and quantity is not None:
            qty = max(qty - quantity, ZERO)
        status = _recompute(qty, status, item)
    elif kind == "low":
        status = "low"
    elif kind == "finished":
        qty, status = ZERO, "out"
    elif kind == "adjusted":
        if quantity is not None:
            qty = quantity
        status = "out" if quantity == ZERO else "in_stock"
    else:
        raise ValueError(f"unknown inventory event type: {event.type}")

    expires_on = None if status == "out" else event.expires_on or row.expires_on
    return StockRow(qty=qty, status=status, expires_on=expires_on)
