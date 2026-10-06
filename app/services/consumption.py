"""Consumption model (spec section 10): how long each item lasts, learned from its own
restock-to-run-out history, and the "probably" shopping list entries that follow from it.

No ML library: an exponentially weighted mean of cycle lengths, newest weighted most.
"""
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import execute, fetch_all
from app.services import households
from app.services.inventory import LIVE_EVENTS

STARTS = ("added", "restocked")
ENDS = ("finished", "low", "discarded")
MIN_CYCLE = timedelta(hours=12)
MAX_CYCLE = timedelta(days=120)
ALPHA = 0.5                        # weight of the newest cycle
MIN_SAMPLES = 2
SOON = timedelta(days=2)           # "probably running low": predicted to run out within this
STALE = timedelta(days=7)          # a run-out predicted this long ago and never confirmed is dropped
ASK_EVERY = timedelta(days=3)      # how often the family may be asked about the same item

# Items whose predicted run-out falls between :oldest and :until, unless the item is really on
# the list already or was taken off it since it was last bought (the family said no once).
_RUNNING_OUT = """consumption_profiles p join items i on i.id = p.item_id
  where i.household_id = :h and p.predicted_runout_at between :oldest and :until
    and not exists (select 1 from shopping_list_items s where s.item_id = p.item_id
                    and ((s.status = 'needed' and s.reason <> 'predicted')
                         or (s.status = 'dismissed' and s.resolved_at >= p.last_restocked_at)))"""


@dataclass(frozen=True)
class Profile:
    avg_days: float | None               # None until one cycle has been seen
    samples: int
    last_restocked_at: datetime | None
    predicted_runout_at: datetime | None   # None unless it is in stock now and 2+ cycles are known


def profile(events: Iterable[tuple[str, datetime]]) -> Profile:
    """Learn one item's profile from its (event_type, occurred_at) history, oldest first.

    A cycle runs from the latest `restocked` or `added` to the next `finished`, `low` or
    `discarded`; cycles under 12 hours or over 120 days are ignored."""
    started = restocked = None
    average: float | None = None
    samples = 0
    for kind, at in events:
        if kind in STARTS:
            started = restocked = at
        elif kind in ENDS and started is not None:
            length, started = at - started, None
            if MIN_CYCLE <= length <= MAX_CYCLE:
                days = length / timedelta(days=1)
                average = days if average is None else ALPHA * days + (1 - ALPHA) * average
                samples += 1
    runout = None
    if started is not None and average is not None and samples >= MIN_SAMPLES:
        runout = started + timedelta(days=average)
    return Profile(average, samples, restocked, runout)


async def refresh(conn: AsyncConnection, household_id: str, now: datetime) -> None:
    """Recompute every item's profile, then make the list's "probably" entries match: one for
    each item predicted to run out within two days, none for a prediction that no longer holds."""
    events = await fetch_all(
        conn,
        f"""select e.item_id, e.event_type, e.occurred_at from {LIVE_EVENTS}
            and e.household_id = :h and e.event_type = any(:kinds) order by e.occurred_at, e.id""",
        h=household_id, kinds=[*STARTS, *ENDS],
    )
    histories: dict[str, list[tuple[str, datetime]]] = {}
    for event in events:
        histories.setdefault(event["item_id"], []).append((event["event_type"], event["occurred_at"]))
    for item_id, history in histories.items():
        learned = profile(history)
        await execute(
            conn,
            """insert into consumption_profiles
                 (item_id, avg_days_to_finish, samples, last_restocked_at, predicted_runout_at, updated_at)
               values (:item, :avg, :samples, :restocked, :runout, :now)
               on conflict (item_id) do update set
                 avg_days_to_finish = excluded.avg_days_to_finish, samples = excluded.samples,
                 last_restocked_at = excluded.last_restocked_at,
                 predicted_runout_at = excluded.predicted_runout_at, updated_at = excluded.updated_at""",
            item=item_id, avg=learned.avg_days, samples=learned.samples, restocked=learned.last_restocked_at,
            runout=learned.predicted_runout_at, now=now,
        )
    await execute(   # an item whose whole history was undone has nothing left to learn from
        conn, "delete from consumption_profiles where not item_id = any(cast(:known as uuid[])) "
              "and item_id in (select id from items where household_id = :h)", known=list(histories), h=household_id)

    window = {"h": household_id, "oldest": now - STALE, "until": now + SOON}
    await execute(
        conn,
        """update shopping_list_items s set status = 'dismissed', resolved_at = :now
           where s.household_id = :h and s.reason = 'predicted' and s.status = 'needed'
             and not exists (select 1 from consumption_profiles p where p.item_id = s.item_id
                             and p.predicted_runout_at between :oldest and :until)""",
        now=now, **window,
    )
    await execute(
        conn,
        f"""insert into shopping_list_items (household_id, item_id, reason, added_at)
            select :h, p.item_id, 'predicted', :now from {_RUNNING_OUT}
            on conflict (household_id, item_id) where status = 'needed' and item_id is not null do nothing""",
        now=now, **window,
    )


async def running_out(conn: AsyncConnection, household_id: str, now: datetime,
                      within: timedelta) -> list[dict[str, Any]]:
    """Items predicted to run out by `now + within` that are not on the list, soonest first."""
    return await fetch_all(
        conn, f"select i.canonical_name as item, p.predicted_runout_at from {_RUNNING_OUT} "
              "order by p.predicted_runout_at, i.canonical_name",
        h=household_id, oldest=now - STALE, until=now + within,
    )


async def to_ask_about(conn: AsyncConnection, household_id: str, now: datetime) -> list[str]:
    """The "probably" entries the family has not been asked about in the last three days.
    Calling this counts as asking."""
    guesses = await fetch_all(
        conn,
        """select s.item_id, i.canonical_name as item from shopping_list_items s join items i on i.id = s.item_id
           where s.household_id = :h and s.reason = 'predicted' and s.status = 'needed' order by i.canonical_name""",
        h=household_id,
    )
    return [guess["item"] for guess in guesses
            if await households.claim_nudge(conn, household_id, f"low_stock:{guess['item_id']}", now,
                                            again_after=ASK_EVERY)]
