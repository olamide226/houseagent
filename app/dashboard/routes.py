"""Dashboard pages and HTMX partials (spec section 13).

Pages read through app/services and write through the same service functions the agent
tools use, logged in agent_actions with source 'dashboard'.
"""
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncConnection

from app.agent import actions
from app.agent.actions import Recorder, record
from app.agent.base import Ctx, ToolError
from app.agent.resolve import Ambiguous, resolve_item, resolve_members
from app.config import get_settings
from app.core.timeutil import day_bounds, local, to_utc, utcnow
from app.dashboard.auth import Session, current_session, require_csrf, templates
from app.db import advisory_lock, engine, fetch_all, fetch_one, tx
from app.pipeline import inbound
from app.pipeline import router as outbound
from app.services import calendar, households, inventory, shopping

router = APIRouter(prefix="/dashboard", default_response_class=HTMLResponse)

ACTIVITY_LIMIT = 200
CALENDAR_DAYS = 30
REPEATS = {"": None, "daily": "FREQ=DAILY", "weekly": "FREQ=WEEKLY", "monthly": "FREQ=MONTHLY"}


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
