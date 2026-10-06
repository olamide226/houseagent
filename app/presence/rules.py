"""Presence rules (spec section 11): what a phone's "entered" or "left" ping sets off.

Pure code, no LLM. Every nudge is claimed in `nudge_log` first, so a replayed or doubled
ping sends nothing twice.
"""
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.envelope import OutboundMessage
from app.core.timeutil import day_bounds, local
from app.db import fetch_all, fetch_val
from app.pipeline.router import enqueue
from app.services import households, shopping

STORE_EVERY = timedelta(hours=2)   # one list per member per store in this long
RECENT = timedelta(hours=2)        # a list someone has just added to is worth offering
BUSY_LIST = 8                      # and so is a long one


async def apply(conn: AsyncConnection, member: dict[str, Any], place: dict[str, Any], event: str,
                now: datetime) -> str | None:
    """Run the rule for one ping. Returns the rule that did something, if any."""
    if place["kind"] == "store" and event == "enter":
        return await _store_arrival(conn, member, place, now)
    if place["kind"] == "home":
        return await (_both_home if event == "enter" else _out_and_about)(conn, member, now)
    return None


async def _store_arrival(conn: AsyncConnection, member: dict[str, Any], place: dict[str, Any],
                         now: datetime) -> str | None:
    """The list for this shop, to whoever just walked in: entries naming it or naming no shop."""
    entries = await shopping.active_entries(conn, member["household_id"], store=place["name"])
    needed = [entry for entry in entries if entry["reason"] != "predicted"]
    probably = [entry for entry in entries if entry["reason"] == "predicted"]
    if not needed or not await households.claim_nudge(
            conn, member["household_id"], f"store:{member['id']}:{place['id']}", now, again_after=STORE_EVERY):
        return None
    text = f"You're at {place['name']}. On the list:\n{shopping.list_text(needed)}"
    if probably:
        text += f"\n\n{shopping.list_text(probably)}"
    await enqueue(conn, OutboundMessage(household_id=member["household_id"], target="member", member_id=member["id"],
                                        text=text, urgency="high"), send_after=now)
    return "store_arrival"


def _out_key(member_id: str, member: dict[str, Any], now: datetime) -> str:
    return f"out:{member_id}:{local(now, member['timezone']).date()}"


async def _out_and_about(conn: AsyncConnection, member: dict[str, Any], now: datetime) -> str | None:
    """Leaving home with a long list, or one just added to: offer it, once a day."""
    entries = await shopping.active_entries(conn, member["household_id"], include_predicted=False)
    if len(entries) < BUSY_LIST and not any(entry["added_at"] > now - RECENT for entry in entries):
        return None
    if not await households.claim_nudge(conn, member["household_id"], _out_key(member["id"], member, now), now):
        return None
    count = f"{len(entries)} item{'' if len(entries) == 1 else 's'}"
    await enqueue(conn, OutboundMessage(
        household_id=member["household_id"], target="member", member_id=member["id"],
        text=f"You're out. The list has {count}, want it?",
        respect_quiet_hours=False,   # they have just walked out of the door: they are awake
    ), send_after=now)
    return "out_and_about"


async def _both_home(conn: AsyncConnection, member: dict[str, Any], now: datetime) -> str | None:
    """Everyone is back and the shop was visited today: nothing is sent, and today's "out and
    about" is marked as used for every adult, so stepping out again brings no late offer."""
    today, _ = day_bounds(local(now, member["timezone"]).date(), member["timezone"])
    shopped = await fetch_val(
        conn, "select exists (select 1 from nudge_log where household_id = :h and dedupe_key like 'store:%' "
              "and sent_at >= :today)", h=member["household_id"], today=today)
    # An adult is out if the last thing their phone said about home is that they left it.
    adults = await fetch_all(
        conn,
        """select m.id, (select pe.event from presence_events pe join places p on p.id = pe.place_id
                         where pe.member_id = m.id and p.kind = 'home'
                         order by pe.occurred_at desc, pe.id desc limit 1) as last_at_home
           from members m where m.household_id = :h and m.role = 'adult'""",
        h=member["household_id"],
    )
    if not shopped or any(adult["last_at_home"] == "exit" for adult in adults):
        return None
    for adult in adults:
        await households.claim_nudge(conn, member["household_id"], _out_key(adult["id"], member, now), now)
    return "both_home"
