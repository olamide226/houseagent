"""Presence endpoint (spec section 11). An iOS Shortcut automation on each adult's phone
posts "entered" or "left" a named place; no app, no coordinates.

    POST /presence/{token}                   {"event": "enter" | "exit", "place": "Tesco Extra"}
    POST /presence/{token}/{event}/{place}   the same ping with nothing to fill in but the link
    GET  either                              a page that says what the link is for; records nothing

The token is personal and only its hash is stored. A POST is always answered 204, whatever the
token or the body, so the endpoint tells a stranger nothing.
"""
import json
from datetime import datetime, timedelta
from urllib.parse import quote

import structlog
from fastapi import APIRouter, Request, Response
from sqlalchemy.ext.asyncio import AsyncConnection

from app.config import get_settings
from app.core.timeutil import utcnow
from app.dashboard.auth import PRIVATE_PAGE, templates
from app.db import execute, fetch_val, tx
from app.presence import rules
from app.services import households, members

log = structlog.get_logger()
router = APIRouter()
EVENTS = ("enter", "exit")
PER_HOUR = 30      # pings accepted per token per hour
PLACE_MAX = 80     # longer than any place name; keeps junk out of `places`


@router.post("/presence/{token}", status_code=204)
async def presence(token: str, request: Request) -> Response:
    try:
        body = json.loads(await request.body())
        event, place = str(body["event"]), str(body["place"])
    except (ValueError, KeyError, TypeError):
        event = place = ""
    return await _accept(token, event, place)


@router.post("/presence/{token}/{event}/{place:path}", status_code=204)
async def presence_named(token: str, event: str, place: str) -> Response:
    """The ping a copied link makes: the event and the place are in the address, so the phone's
    Shortcut holds one pasted link and nothing else."""
    return await _accept(token, event, place)


async def _accept(token: str, event: str, place: str) -> Response:
    event, place = event.strip().lower(), " ".join(place.split())
    if event in EVENTS and 0 < len(place) <= PLACE_MAX:
        try:
            async with tx() as conn:
                await ping(conn, token, event, place, utcnow())
        except Exception:   # still 204: an error only a valid token can cause would give the token away
            log.exception("presence_failed")
    else:
        log.info("presence_ignored", reason="bad body")
    return Response(status_code=204)


@router.get("/presence/{token}")
@router.get("/presence/{token}/{event}/{place:path}")
async def presence_page(request: Request, token: str) -> Response:
    """What someone sees who opens their link, or one copied from it, in a browser: what it is
    for and how to put it on an iPhone. Only reads; a ping is always a POST."""
    async with tx() as conn:
        member = await members.for_presence_token(conn, token)
        places = await households.places(conn, member["household_id"]) if member else []
    settings = get_settings()

    def link(event: str, place: str) -> str:
        return f"{households.presence_url(token)}/{event}/{quote(place, safe='')}"

    home = next((place["name"] for place in places if place["kind"] == "home"), "Home")
    return templates.TemplateResponse(request, "presence.html", {
        "agent": settings.agent_name, "known": member is not None, "shortcut": settings.presence_shortcut_url,
        "admin": bool(member and member["is_admin"]),
        "shops": [(place["name"], link("enter", place["name"])) for place in places if place["kind"] == "store"],
        "home": [("Arriving home", link("enter", home)), ("Leaving home", link("exit", home))],
    }, status_code=200 if member else 404, headers=PRIVATE_PAGE)


async def ping(conn: AsyncConnection, token: str, event: str, place_name: str, now: datetime) -> str | None:
    """Record one ping from the phone holding `token` and run the rules. Returns the rule that fired."""
    member = await members.for_presence_token(conn, token)
    if member is None:
        log.info("presence_ignored", reason="unknown token")
        return None
    recent = await fetch_val(conn, "select count(*) from presence_events where member_id = :m and occurred_at > :since",
                             m=member["id"], since=now - timedelta(hours=1))
    if recent >= PER_HOUR:
        log.warning("presence_rate_limited", household_id=member["household_id"])
        return None
    place = await households.place_named(conn, member["household_id"], place_name)
    await execute(conn, "insert into presence_events (member_id, place_id, event, occurred_at) "
                        "values (:m, :place, :event, :now)", m=member["id"], place=place["id"], event=event, now=now)
    rule = await rules.apply(conn, member, place, event, now)
    log.info("presence", household_id=member["household_id"], place_kind=place["kind"], presence_event=event, rule=rule)
    return rule
