"""Dashboard pages and HTMX partials (spec section 13).

Pages read through app/services and write through the same service functions the agent
tools use, logged in agent_actions with source 'dashboard'.
"""
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncConnection

from app.agent import actions
from app.agent.actions import Recorder, record
from app.agent.base import Ctx, ToolError
from app.agent.resolve import Ambiguous, resolve_item, resolve_members
from app.channels.base import ADAPTERS, ChannelError, GroupHost
from app.config import get_settings
from app.core.envelope import Channel, GroupUpdate
from app.core.timeutil import day_bounds, local, parse_clock, to_utc, utcnow
from app.dashboard.auth import Session, current_session, invite_context, qr_svg, require_csrf, templates
from app.db import advisory_lock, engine, fetch_all, fetch_one, tx
from app.pipeline import inbound
from app.pipeline import router as outbound
from app.services import calendar, households, inventory, members, shopping

router = APIRouter(prefix="/dashboard", default_response_class=HTMLResponse)

ACTIVITY_LIMIT = 200
CALENDAR_DAYS = 30
REPEATS = {"": None, "daily": "FREQ=DAILY", "weekly": "FREQ=WEEKLY", "monthly": "FREQ=MONTHLY"}
BUILT_CHANNELS = (Channel.telegram, Channel.whatsapp)   # the channels that have an adapter


def _ctx(conn: AsyncConnection, session: Session) -> Ctx:
    return Ctx(conn=conn, household_id=session.household_id, member_id=session.member_id, source="dashboard")


async def _write(session: Session, name: str, args: dict[str, Any],
                 action: Callable[[Recorder], Awaitable[None]]) -> str | None:
    """Run one dashboard write as a logged, undoable action. Returns an error message, if any."""
    try:
        async with tx() as conn, record(_ctx(conn, session), name, args) as rec:
            await action(rec)
    except ToolError as exc:
        return str(exc)
    return None


def _page(request: Request, template: str, session: Session, **context: Any) -> Response:
    return templates.TemplateResponse(request, template, {"session": session, **context})


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value) if value.strip() else None
    except InvalidOperation:
        raise ToolError(f"'{value}' is not a number") from None


# ---------------------------------------------------------------- Today
@router.get("")
async def today(request: Request, session: Session = Depends(current_session)) -> Response:
    now = utcnow()
    today = local(now, session.timezone)
    _, tomorrow_night = day_bounds(today.date() + timedelta(days=1), session.timezone)
    async with tx() as conn:
        context = {
            "today": today,
            "days": _by_day(await calendar.occurrences_between(conn, session.household_id, now, tomorrow_night),
                            await calendar.standalone_reminders(conn, session.household_id, now, tomorrow_night),
                            session.timezone),
            "list_count": len(await shopping.active_entries(conn, session.household_id, include_predicted=False)),
            "low": await inventory.stock_rows(conn, session.household_id, statuses=["low", "out"]),
            "expiring": await inventory.stock_rows(conn, session.household_id, expiring_within_days=3,
                                                   today=today.date()),
        }
    return _page(request, "today.html", session, **context)


def _by_day(occurrences: list[calendar.Occurrence], reminders: list[dict[str, Any]],
            timezone: str) -> dict[date, list[dict[str, Any]]]:
    """Events and standalone reminders as one time-ordered list per household-local day."""
    entries: list[dict[str, Any]] = [{"at": local(o.start, timezone), "event": o} for o in occurrences]
    entries += [{"at": local(r["fire_at"], timezone), "reminder": r} for r in reminders]
    days: dict[date, list[dict[str, Any]]] = {}
    for entry in sorted(entries, key=lambda entry: entry["at"]):
        days.setdefault(entry["at"].date(), []).append(entry)
    return days


# ---------------------------------------------------------------- Shopping list
async def _shopping(request: Request, session: Session, template: str = "_shopping.html",
                    error: str | None = None) -> Response:
    async with tx() as conn:
        entries = await shopping.active_entries(conn, session.household_id)
    return _page(request, template, session, error=error,
                 needed=[e for e in entries if e["reason"] != "predicted"],
                 predicted=[e for e in entries if e["reason"] == "predicted"])


@router.get("/shopping")
async def shopping_page(request: Request, session: Session = Depends(current_session)) -> Response:
    return await _shopping(request, session, "shopping.html")


@router.post("/shopping/add")
async def shopping_add(request: Request, item: str = Form(...), store_hint: str = Form(""),
                       session: Session = Depends(require_csrf)) -> Response:
    async def add(rec: Recorder) -> None:
        found = await resolve_item(rec.ctx.conn, session.household_id, item)
        if isinstance(found, Ambiguous):
            raise ToolError(f"'{item}' could be {', '.join(found.options)}. Use the full name.")
        await shopping.add(rec, found.id, found.name, store_hint=store_hint.strip() or None)

    error = await _write(session, "shopping.add", {"item": item, "store_hint": store_hint}, add)
    if request.headers.get("HX-Request"):
        return await _shopping(request, session, error=error)
    # The Today quick-add is a plain form post: back to Today, or to the list to show what went wrong.
    if error is None:
        return RedirectResponse("/dashboard", status_code=303)
    return await _shopping(request, session, "shopping.html", error)


@router.post("/shopping/{entry_id}/{verb}")
async def shopping_change(request: Request, entry_id: str, verb: str, store_hint: str = Form(""),
                          session: Session = Depends(require_csrf)) -> Response:
    async def change(rec: Recorder) -> None:
        entry = await fetch_one(
            rec.ctx.conn,
            """select s.item_id, i.canonical_name from shopping_list_items s join items i on i.id = s.item_id
               where s.id = :id and s.household_id = :h and s.status = 'needed'""",
            id=entry_id, h=session.household_id,
        )
        if entry is None:
            raise ToolError("that entry is no longer on the list")
        if verb == "bought":
            await shopping.bought(rec, entry["item_id"])
        elif verb == "remove":
            await shopping.remove(rec, entry["item_id"], entry["canonical_name"])
        elif verb == "store":
            await shopping.set_store_hint(rec, entry_id, store_hint.strip())
        else:
            raise ToolError("unknown action")

    error = await _write(session, f"shopping.{verb}", {"entry_id": entry_id, "store_hint": store_hint}, change)
    return await _shopping(request, session, error=error)


# ---------------------------------------------------------------- Inventory
async def _inventory(request: Request, session: Session, template: str, status: str = "",
                     error: str | None = None) -> Response:
    async with tx() as conn:
        rows = await inventory.stock_rows(conn, session.household_id, statuses=[status] if status else None)
    by_location: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_location.setdefault(row["location"], []).append(row)
    return _page(request, template, session, by_location=by_location, status=status, error=error,
                 quantity_text=inventory.quantity_text)


@router.get("/inventory")
async def inventory_page(request: Request, status: str = "", session: Session = Depends(current_session)) -> Response:
    template = "_inventory.html" if request.headers.get("HX-Request") else "inventory.html"
    return await _inventory(request, session, template, status)


async def _item(request: Request, session: Session, item_id: str, error: str | None = None) -> Response:
    async with tx() as conn:
        item = await fetch_one(conn, "select * from items where id = :id and household_id = :h",
                               id=item_id, h=session.household_id)
        if item is None:
            return Response(status_code=404)
        context = {
            "item": item,
            "stock": await inventory.stock_rows(conn, session.household_id, item_id=item_id),
            "locations": await fetch_all(conn, "select id, name from locations where household_id = :h order by name",
                                         h=session.household_id),
            "others": await fetch_all(
                conn, "select id, canonical_name from items where household_id = :h and id <> :id "
                      "order by canonical_name", h=session.household_id, id=item_id),
            "history": await fetch_all(
                conn,
                """select e.event_type, e.quantity, e.unit, e.source, e.occurred_at, l.name as location,
                          m.name as member
                   from inventory_events e left join locations l on l.id = e.location_id
                   left join members m on m.id = e.member_id
                   where e.item_id = :id order by e.occurred_at desc limit 50""", id=item_id),
        }
    return _page(request, "item.html", session, error=error, quantity_text=inventory.quantity_text, **context)


@router.get("/inventory/items/{item_id}")
async def item_page(request: Request, item_id: str, session: Session = Depends(current_session)) -> Response:
    return await _item(request, session, item_id)


@router.post("/inventory/items/{item_id}/stock")
async def item_stock(request: Request, item_id: str, action: str = Form(...), location_id: str = Form(...),
                     quantity: str = Form(""), session: Session = Depends(require_csrf)) -> Response:
    async def change(rec: Recorder) -> None:
        if action not in ("adjusted", "finished"):
            raise ToolError("unknown action")
        amount = _decimal(quantity) if action == "adjusted" else None
        await inventory.apply_change(rec, inventory.Change(item_id, action, amount, location_id=location_id), "message")

    args = {"item_id": item_id, "action": action, "location_id": location_id, "quantity": quantity}
    return await _item(request, session, item_id, await _write(session, f"inventory.{action}", args, change))


@router.post("/inventory/items/{item_id}/edit")
async def item_edit(request: Request, item_id: str, aliases: str = Form(""), is_staple: bool = Form(False),
                    low_threshold: str = Form(""), default_location_id: str = Form(""),
                    session: Session = Depends(require_csrf)) -> Response:
    async def edit(rec: Recorder) -> None:
        await inventory.update_item(
            rec, item_id, aliases=sorted({a.strip().lower() for a in aliases.split(",") if a.strip()}),
            is_staple=is_staple, low_threshold=_decimal(low_threshold),
            default_location_id=default_location_id or None,
        )

    args = {"item_id": item_id, "aliases": aliases, "is_staple": is_staple, "low_threshold": low_threshold,
            "default_location_id": default_location_id}
    return await _item(request, session, item_id, await _write(session, "inventory.edit_item", args, edit))


@router.post("/inventory/items/{item_id}/merge")
async def item_merge(request: Request, item_id: str, duplicate_id: str = Form(...),
                     session: Session = Depends(require_csrf)) -> Response:
    async def merge(rec: Recorder) -> None:
        await inventory.merge_items(rec, item_id, duplicate_id)

    args = {"keep_id": item_id, "duplicate_id": duplicate_id}
    return await _item(request, session, item_id, await _write(session, "inventory.merge_items", args, merge))


# ---------------------------------------------------------------- Calendar
async def _calendar(request: Request, session: Session, template: str = "_calendar.html",
                    error: str | None = None, feed: str | None = None) -> Response:
    now = utcnow()
    until = now + timedelta(days=CALENDAR_DAYS)
    async with tx() as conn:
        coming = await calendar.occurrences_between(conn, session.household_id, now, until)
        context = {
            "days": _by_day(coming, [], session.timezone),
            "series": await calendar.active_events(conn, session.household_id, recurring=True),
            "reminders": await calendar.standalone_reminders(conn, session.household_id, now, until, expand=False),
        }
    return _page(request, template, session, error=error, feed=feed, local=local, repeats=REPEATS, **context)


def _when(value: str, timezone: str) -> datetime:
    try:
        return to_utc(datetime.fromisoformat(value), timezone)
    except ValueError:
        raise ToolError("pick a date and a time") from None


@router.get("/calendar")
async def calendar_page(request: Request, session: Session = Depends(current_session)) -> Response:
    return await _calendar(request, session, "calendar.html")


@router.post("/calendar/add")
async def calendar_add(request: Request, title: str = Form(...), when: str = Form(...), repeat: str = Form(""),
                       who: str = Form(""), location: str = Form(""),
                       session: Session = Depends(require_csrf)) -> Response:
    async def add(rec: Recorder) -> None:
        names = [name.strip() for name in who.split(",") if name.strip()]
        people, unknown = await resolve_members(rec.ctx.conn, session.household_id, names, session.member_id)
        if unknown:
            raise ToolError(f"nobody called {', '.join(unknown)} is in the family")
        await calendar.schedule_event(
            rec, title=title, starts_at=_when(when, session.timezone), rrule=REPEATS.get(repeat),
            kind="activity" if REPEATS.get(repeat) else "appointment", participant_ids=people, location=location)

    args = {"title": title, "when": when, "repeat": repeat, "who": who, "location": location}
    return await _calendar(request, session, error=await _write(session, "calendar.add", args, add))


@router.post("/calendar/events/{event_id}/{verb}")
async def calendar_change(request: Request, event_id: str, verb: str, title: str = Form(""), when: str = Form(""),
                          location: str = Form(""), day: str = Form(""),
                          session: Session = Depends(require_csrf)) -> Response:
    async def change(rec: Recorder) -> None:
        if verb == "edit":
            await calendar.modify_event(rec, event_id, scope="all", title=title,
                                        starts_at=_when(when, session.timezone), location=location)
        elif verb == "cancel":
            await calendar.modify_event(rec, event_id, scope="all", cancel=True)
        elif verb == "skip":
            try:
                await calendar.modify_event(rec, event_id, scope="this", cancel=True,
                                            occurrence=date.fromisoformat(day))
            except ValueError:
                raise ToolError("that is not a date") from None
        else:
            raise ToolError("unknown action")

    args = {"event_id": event_id, "title": title, "when": when, "location": location, "day": day}
    return await _calendar(request, session, error=await _write(session, f"calendar.{verb}", args, change))


@router.post("/calendar/reminders/{reminder_id}/cancel")
async def calendar_reminder_cancel(request: Request, reminder_id: str,
                                   session: Session = Depends(require_csrf)) -> Response:
    async def cancel(rec: Recorder) -> None:
        await calendar.cancel_reminder(rec, reminder_id)

    error = await _write(session, "calendar.cancel_reminder", {"reminder_id": reminder_id}, cancel)
    return await _calendar(request, session, error=error)


@router.post("/calendar/feed")
async def calendar_feed(request: Request, session: Session = Depends(require_csrf)) -> Response:
    """A new subscribe link. Only the token's hash is kept, so the link is shown this once
    and any earlier link stops working."""
    async with tx() as conn:
        token = await households.new_calendar_token(conn, session.household_id)
    return await _calendar(request, session, feed=f"{get_settings().public_base_url}/ics/{token}.ics")


# ---------------------------------------------------------------- Family
async def _family(request: Request, session: Session, template: str = "_family.html", error: str | None = None,
                  invite: dict[str, str | None] | None = None) -> Response:
    async with tx() as conn:
        family = await members.family(conn, session.household_id, utcnow())
    return _page(request, template, session, family=family, error=error, invite=invite, local=local)


@router.get("/family")
async def family_page(request: Request, session: Session = Depends(current_session)) -> Response:
    return await _family(request, session, "family.html")


@router.post("/family/add")
async def family_add(request: Request, name: str = Form(...), role: str = Form("adult"),
                     session: Session = Depends(require_csrf)) -> Response:
    """Add a child, or an adult with an invite to pass on. The invite is shown this once."""
    shown: dict[str, str | None] = {}

    async def add(rec: Recorder) -> None:
        member, created = await members.add_member(rec, name, role)
        if not created:
            raise ToolError(f"{member['name']} is already in the family")
        if role == "adult":
            shown.update(invite_context(member["name"], await members.invite(rec, member["id"])))

    error = await _write(session, "family.add", {"name": name, "role": role}, add)
    return await _family(request, session, error=error, invite=shown if shown and not error else None)


@router.post("/family/{member_id}/{verb}")
async def family_change(request: Request, member_id: str, verb: str, channel: str = Form(""),
                        session: Session = Depends(require_csrf)) -> Response:
    shown: dict[str, str | None] = {}

    async def change(rec: Recorder) -> None:
        if verb == "invite":
            code = await members.invite(rec, member_id)
            shown.update(invite_context(await members.name_of(rec.ctx.conn, member_id), code))
        elif verb == "revoke":
            await members.revoke_invite(rec, member_id)
        elif verb == "channel":
            await members.set_preferred_channel(rec, member_id, channel)
        else:
            raise ToolError("unknown action")

    error = await _write(session, f"family.{verb}", {"member_id": member_id, "channel": channel}, change)
    return await _family(request, session, error=error, invite=shown if shown and not error else None)


# ---------------------------------------------------------------- Channels
async def _channels(request: Request, session: Session, template: str = "_channels.html",
                    error: str | None = None, invite: dict[str, str] | None = None) -> Response:
    now = utcnow()
    async with tx() as conn:
        chats = await households.threads(conn, session.household_id)
        people = await members.identities(conn, session.household_id)
        failed = await outbound.failed_since(conn, session.household_id, now - timedelta(days=1))
        last_sent = await outbound.last_sent(conn, session.household_id)
    owners = {(p["channel"], ADAPTERS[Channel(p["channel"])].dm_thread_id(p["handle"])): p
              for p in people if Channel(p["channel"]) in ADAPTERS}
    for chat in chats:
        adapter = ADAPTERS.get(Channel(chat["channel"]))
        owner = owners.get((chat["channel"], chat["external_thread_id"]))
        chat["who"] = owner["name"] if owner else None
        chat["pending"] = chat["external_thread_id"].startswith(households.PENDING)
        chat["name"] = chat["external_thread_id"].removeprefix(households.PENDING) if chat["pending"] else None
        chat["invitable"] = chat["scope"] == "group" and not chat["pending"] and isinstance(adapter, GroupHost)
        # Where a channel only takes free-form text for some hours after it last heard from someone.
        hours = adapter.capabilities.proactive_window_hours if adapter else None
        # Connecting counts as being heard from, as it does for the router's window.
        heard = chat["heard"] = max(filter(None, [chat["last_in"], owner["verified_at"] if owner else None]),
                                    default=None)
        chat["window"] = hours and {"open_until": heard + timedelta(hours=hours)
                                    if heard and heard + timedelta(hours=hours) > now else None}
    health = [{
        "channel": channel.value, "on": channel in ADAPTERS,
        "heard": max((c["heard"] for c in chats if c["channel"] == channel.value and c["heard"]), default=None),
        "last_sent": last_sent.get(channel.value), "failed": failed.get(channel.value, 0),
        "template": ADAPTERS[channel].capabilities.proactive_template if channel in ADAPTERS else None,
        "creates_groups": isinstance(ADAPTERS.get(channel), GroupHost),
    } for channel in BUILT_CHANNELS]
    return _page(request, template, session, health=health, chats=chats, error=error, invite=invite, local=local)


def _group_host(channel: str) -> GroupHost:
    adapter = ADAPTERS.get(Channel(channel)) if channel in set(Channel) else None
    if not isinstance(adapter, GroupHost):
        raise ToolError(f"{channel} cannot create groups or is not set up here")
    return adapter


@router.get("/channels")
async def channels_page(request: Request, session: Session = Depends(current_session)) -> Response:
    return await _channels(request, session, "channels.html")


@router.post("/channels/threads/{thread_id}/primary")
async def channels_primary(request: Request, thread_id: str,
                           session: Session = Depends(require_csrf)) -> Response:
    async def change(rec: Recorder) -> None:
        await households.set_primary_thread(rec, thread_id)

    return await _channels(request, session,
                           error=await _write(session, "channels.primary", {"thread_id": thread_id}, change))


@router.post("/channels/threads/{thread_id}/invite")
async def channels_invite(request: Request, thread_id: str, session: Session = Depends(require_csrf)) -> Response:
    """Ask the channel for the group's invite link and show it with a QR code. Nothing is stored."""
    try:
        async with tx() as conn:
            thread = await households.group_thread(conn, session.household_id, thread_id)
        link = await _group_host(thread["channel"]).invite_link(thread["external_thread_id"])
    except (ToolError, ChannelError) as exc:
        return await _channels(request, session, error=str(exc))
    return await _channels(request, session, invite={"link": link, "qr": qr_svg(link)})


@router.post("/channels/{channel}/group")
async def channels_group(request: Request, channel: str, subject: str = Form(...),
                         session: Session = Depends(require_csrf)) -> Response:
    """Ask the channel to create the family group. It becomes the primary thread once the
    channel confirms it, which for WhatsApp arrives by webhook a moment later."""
    async def create(rec: Recorder) -> None:
        host = _group_host(channel)
        name = await households.start_group(rec, channel, subject)
        try:
            group_id = await host.create_group(name)
        except ChannelError as exc:
            raise ToolError(str(exc)) from None
        if group_id:
            await households.finish_group(rec.ctx.conn, GroupUpdate(
                channel=Channel(channel), subject=name, external_thread_id=group_id))

    args = {"channel": channel, "subject": subject}
    return await _channels(request, session, error=await _write(session, "channels.group", args, create))


# ---------------------------------------------------------------- Settings
async def _settings(request: Request, session: Session, template: str = "_settings.html",
                    error: str | None = None) -> Response:
    async with tx() as conn:
        context = {
            "family": await members.family(conn, session.household_id, utcnow()),
            "facts": await households.facts(conn, session.household_id),
            "digest_time": await households.digest_time(conn, session.household_id),
        }
    return _page(request, template, session, error=error, **context)


def _clock(value: str) -> time | None:
    try:
        return parse_clock(value) if value.strip() else None
    except ValueError:
        raise ToolError(f"'{value}' is not a time of day") from None


@router.get("/settings")
async def settings_page(request: Request, session: Session = Depends(current_session)) -> Response:
    return await _settings(request, session, "settings.html")


@router.post("/settings/brief")
async def settings_brief(request: Request, at: str = Form(...), session: Session = Depends(require_csrf)) -> Response:
    async def change(rec: Recorder) -> None:
        when = _clock(at)
        if when is None:
            raise ToolError("the morning brief needs a time")
        await households.set_digest_time(rec, when)

    return await _settings(request, session, error=await _write(session, "settings.brief", {"at": at}, change))


@router.post("/settings/quiet/{member_id}")
async def settings_quiet(request: Request, member_id: str, start: str = Form(""), end: str = Form(""),
                         session: Session = Depends(require_csrf)) -> Response:
    async def change(rec: Recorder) -> None:
        await members.set_quiet_hours(rec, [member_id], _clock(start), _clock(end))

    args = {"member_id": member_id, "start": start, "end": end}
    return await _settings(request, session, error=await _write(session, "settings.quiet_hours", args, change))


@router.post("/settings/facts")
async def settings_fact(request: Request, key: str = Form(...), value: str = Form(""), member_id: str = Form(""),
                        session: Session = Depends(require_csrf)) -> Response:
    """Add, change or (with no value) forget a fact."""
    async def change(rec: Recorder) -> None:
        await households.set_fact(rec, key, value, member_id or None)

    args = {"key": key, "value": value, "member_id": member_id}
    return await _settings(request, session, error=await _write(session, "settings.fact", args, change))


# ---------------------------------------------------------------- Activity
async def _activity(request: Request, session: Session, template: str, error: str | None = None) -> Response:
    async with tx() as conn:
        turns = await fetch_all(
            conn,
            """select m.id, m.text, m.media, m.meta, m.status, m.created_at, mem.name as member, t.channel, t.scope,
                      o.status as outbox_status, o.id as outbox_id, o.text as reply, o.react_emoji, o.last_error
               from messages m join threads t on t.id = m.thread_id left join members mem on mem.id = m.member_id
               left join outbox o on o.id = cast(m.meta->'turn'->>'outbox_id' as uuid)
               where m.household_id = :h and m.direction = 'in'
               order by m.created_at desc limit :limit""",
            h=session.household_id, limit=ACTIVITY_LIMIT,
        )
        logged = await fetch_all(
            conn,
            """select a.id, a.message_id, a.source, a.tool, a.result, a.created_at, a.undone_at,
                      a.inverse <> '[]' as undoable, mem.name as member
               from agent_actions a left join members mem on mem.id = a.member_id
               where a.household_id = :h order by a.created_at desc limit :limit""",
            h=session.household_id, limit=ACTIVITY_LIMIT * 3,
        )
    by_message: dict[str, list[dict[str, Any]]] = {}
    for action in logged:
        if action["message_id"]:
            by_message.setdefault(action["message_id"], []).append(action)
    feed = [{"kind": "turn", "at": t["created_at"], **t, "actions": by_message.get(t["id"], [])} for t in turns]
    feed += [{"kind": "action", "at": a["created_at"], **a} for a in logged if not a["message_id"]]
    feed.sort(key=lambda entry: entry["at"], reverse=True)
    return _page(request, template, session, feed=feed[:ACTIVITY_LIMIT], error=error)


@router.get("/activity")
async def activity_page(request: Request, session: Session = Depends(current_session)) -> Response:
    return await _activity(request, session, "activity.html")


@router.post("/activity/actions/{action_id}/undo")
async def activity_undo(request: Request, action_id: str, session: Session = Depends(require_csrf)) -> Response:
    error = None
    try:
        async with tx() as conn:
            await advisory_lock(conn, session.household_id)
            await actions.undo_action(_ctx(conn, session), action_id)
    except ToolError as exc:
        error = str(exc)
    return await _activity(request, session, "_activity.html", error)


@router.post("/activity/outbox/{outbox_id}/retry")
async def activity_retry(request: Request, outbox_id: str, session: Session = Depends(require_csrf)) -> Response:
    async with tx() as conn:
        await outbound.retry(conn, session.household_id, outbox_id)
    return await _activity(request, session, "_activity.html")


# ---------------------------------------------------------------- Playground
@router.get("/playground")
async def playground_page(request: Request, session: Session = Depends(current_session)) -> Response:
    return _page(request, "playground.html", session)


@router.post("/playground")
async def playground_turn(request: Request, text: str = Form(...), apply: bool = Form(False),
                          session: Session = Depends(require_csrf)) -> Response:
    """Run a turn through simulate_turn. Dry run by default: the whole turn is rolled back."""
    error = result = None
    async with engine().connect() as conn:
        transaction = await conn.begin()
        try:
            await advisory_lock(conn, session.household_id)
            result = await inbound.simulate_turn(conn, request.app.state.runtime, session.household_id,
                                                 session.member_id, text)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        if apply and error is None:
            await transaction.commit()
        else:
            await transaction.rollback()
    return _page(request, "_playground_turn.html", session, text=text, result=result, error=error, applied=apply)
