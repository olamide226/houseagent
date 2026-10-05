"""Every row of the stock transition table (spec section 4)."""
from datetime import date
from decimal import Decimal as D

import pytest

from app.agent.stock import Item, StockEvent, StockRow, apply

NO_ITEM = Item()
CASES = [
    # (name, row before, event, low_threshold, expected qty, expected status)
    ("added adds when both known", StockRow(D(2), "low"), StockEvent("added", D(4)), None, D(6), "in_stock"),
    ("restocked adds when both known", StockRow(D(0), "out"), StockEvent("restocked", D(12)), None, D(12), "in_stock"),
    ("restocked with unknown quantity keeps qty", StockRow(D(0), "out"), StockEvent("restocked"), None, D(0), "in_stock"),
    ("restocked onto unknown stock stays null", StockRow(None, "low"), StockEvent("restocked", D(6)), None, None, "in_stock"),
    ("added to a fresh row stays null", None, StockEvent("added", D(6)), None, None, "in_stock"),
    ("used subtracts", StockRow(D(6), "in_stock"), StockEvent("used", D(2)), None, D(4), "in_stock"),
    ("used floors at zero and is out", StockRow(D(1), "in_stock"), StockEvent("used", D(3)), None, D(0), "out"),
    ("used down to the threshold is low", StockRow(D(6), "in_stock"), StockEvent("used", D(4)), D(2), D(2), "low"),
    ("used with unknown quantity keeps status", StockRow(D(6), "in_stock"), StockEvent("used"), None, D(6), "in_stock"),
    ("used on unknown stock keeps previous status", StockRow(None, "low"), StockEvent("used", D(1)), None, None, "low"),
    ("used on a fresh row is unknown", None, StockEvent("used", D(1)), None, None, "unknown"),
    ("low keeps qty", StockRow(D(3), "in_stock"), StockEvent("low"), None, D(3), "low"),
    ("low on a fresh row", None, StockEvent("low"), None, None, "low"),
    ("finished zeroes", StockRow(D(6), "in_stock"), StockEvent("finished"), None, D(0), "out"),
    ("finished on unknown stock", StockRow(None, "in_stock"), StockEvent("finished"), None, D(0), "out"),
    ("discarded subtracts like used", StockRow(D(6), "in_stock"), StockEvent("discarded", D(2)), None, D(4), "in_stock"),
    ("discarded to zero is out", StockRow(D(2), "in_stock"), StockEvent("discarded", D(2)), None, D(0), "out"),
    ("discarded without quantity means finished", StockRow(D(6), "in_stock"), StockEvent("discarded"), None, D(0), "out"),
    ("adjusted sets the absolute count", StockRow(D(6), "low"), StockEvent("adjusted", D(3)), None, D(3), "in_stock"),
    ("adjusted to zero is out", StockRow(D(6), "in_stock"), StockEvent("adjusted", D(0)), None, D(0), "out"),
    ("adjusted without quantity keeps qty", StockRow(D(6), "low"), StockEvent("adjusted"), None, D(6), "in_stock"),
    ("adjusted on a fresh row", None, StockEvent("adjusted", D(4)), None, D(4), "in_stock"),
]


@pytest.mark.parametrize("name,row,event,threshold,qty,status", CASES, ids=[c[0] for c in CASES])
def test_transition(name, row, event, threshold, qty, status):
    after = apply(row, event, Item(threshold))
    assert (after.qty, after.status) == (qty, status)


def test_every_event_type_in_the_schema_is_handled():
    for kind in ("added", "used", "low", "finished", "restocked", "adjusted", "discarded"):
        apply(None, StockEvent(kind), NO_ITEM)
    with pytest.raises(ValueError):
        apply(None, StockEvent("eaten"), NO_ITEM)


def test_expiry_follows_the_newest_batch_and_clears_when_out():
    row = apply(None, StockEvent("restocked", expires_on=date(2026, 10, 8)), NO_ITEM)
    assert row.expires_on == date(2026, 10, 8)
    assert apply(row, StockEvent("used"), NO_ITEM).expires_on == date(2026, 10, 8)
    assert apply(row, StockEvent("restocked", expires_on=date(2026, 10, 20)), NO_ITEM).expires_on == date(2026, 10, 20)
    assert apply(row, StockEvent("finished"), NO_ITEM).expires_on is None
