"""Two-way sync between the household's events and the calendars adults connect (ADR 0038).

The household's `events` table stays the source of truth. A connected calendar is a second copy
of it: each round reads what changed there and applies it here through `services/calendar.py`,
then sends what differs here over there. `calendar_links.synced` holds each event as both sides
last agreed on it, which is how a change is told from an echo of our own write.

A round never holds the household's lock while it talks to the provider: it reads, then applies
under the lock, then sends, then notes what was sent.
"""
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncConnection

from app.agent.actions import Recorder, record
from app.agent.base import Ctx, ToolError
from app.calendars import apple, google
from app.calendars.base import (
    DEFAULT_LENGTH,
    AuthLost,
    CalendarError,
    Entry,
    Gone,
    Provider,
    Refused,
    Remote,
    RemoteCalendar,
    SyncReset,
    canonical_rrule,
)
from app.config import Settings
from app.core import crypto
from app.core.envelope import OutboundMessage
from app.core.timeutil import day_bounds, local, next_occurrence
from app.db import advisory_lock, execute, fetch_all, fetch_one, fetch_val, jsonb, tx
from app.pipeline.router import enqueue
from app.services import calendar, members

log = structlog.get_logger()
PROVIDERS = {"google": "Google Calendar", "apple": "Apple Calendar"}    # as a person would name each
PULL_EVERY = timedelta(minutes=2)            # how often a calendar is asked what changed
LEASE = timedelta(minutes=5)                 # a round that died is picked up again after this
FIRST_WAIT = timedelta(seconds=30)           # after a failure; doubled each time, up to MAX_WAIT
MAX_WAIT = timedelta(hours=1)
SEND_AT_ONCE = 50                            # events sent in one round; the rest go in the next
KEEP_ENDED = timedelta(days=30)              # a one-off this long over is no longer followed
STOPPED = ("I can no longer reach your {calendar}, so plans are not being copied between it and me. To fix it, "
           "send me the word dashboard, open Calendar and tap Connect again.")
# Apple shows a password once, so connecting again takes a new one.
STOPPED_MORE = {"apple": " It needs a new app-specific password from Apple: the page shows how to make one."}


# ---------------------------------------------------------------- connecting
def label(connection: dict[str, Any]) -> str:
    return f"{connection['member']}'s {PROVIDERS[connection['provider']]}"


async def connections(conn: AsyncConnection, household_id: str) -> list[dict[str, Any]]:
    """Each adult's connected calendar, without its sign-in."""
    return await fetch_all(
        conn,
        """select c.id, c.member_id, m.name as member, c.provider, c.account, c.status, c.choices, c.calendar_id,
                  c.calendar_name, c.last_synced_at, c.last_error, c.failures
           from calendar_connections c join members m on m.id = c.member_id
           where c.household_id = :h order by m.created_at, m.id""", h=household_id)


async def begin(conn: AsyncConnection, household_id: str, member_id: str, provider: str, account: str | None,
                secret: str, choices: list[RemoteCalendar]) -> None:
    """Someone has signed in: keep the sign-in (already encrypted) and the calendars to pick from.
    Nothing is copied until one is picked. Signing in again keeps what was linked before, in case
    the same calendar is picked again."""
    await execute(
        conn,
        """insert into calendar_connections (household_id, member_id, provider, account, secret, choices)
           values (:h, :m, :provider, :account, :secret, cast(:choices as jsonb))
           on conflict (member_id) do update set provider = excluded.provider, account = excluded.account,
             secret = excluded.secret, choices = excluded.choices, status = 'choosing', last_error = null,
             failures = 0, leased_until = null,
             calendar_id = case when calendar_connections.provider = excluded.provider
                                then calendar_connections.calendar_id end""",
        h=household_id, m=member_id, provider=provider, account=account, secret=secret,
        choices=jsonb([{"id": choice.id, "name": choice.name} for choice in choices]))


async def choose(rec: Recorder, calendar_id: str) -> None:
    """Pick the one calendar to share with the household: from now on it is kept in step."""
    conn, household_id, member_id = rec.ctx.conn, rec.ctx.household_id, rec.ctx.member_id
    mine = await fetch_one(
        conn, "select id, provider, choices, calendar_id from calendar_connections "
              "where member_id = :m and household_id = :h and status = 'choosing' for update",
        m=member_id, h=household_id)
    choice = next((choice for choice in (mine or {}).get("choices", []) if choice["id"] == calendar_id), None)
    if mine is None or choice is None:
        raise ToolError("pick one of the calendars listed")
    taken = await fetch_val(
        conn,
        """select m.name from calendar_connections c join members m on m.id = c.member_id
           where c.household_id = :h and c.provider = :provider and c.calendar_id = :calendar and c.id <> :id""",
        h=household_id, provider=mine["provider"], calendar=calendar_id, id=mine["id"])
    if taken:
        raise ToolError(f"{taken} already shares that calendar with the household, so its plans are here already. "
                        "Pick a different one.")
    if mine["calendar_id"] != calendar_id:   # a different calendar: nothing on it is linked to anything here
        await execute(conn, "delete from calendar_links where connection_id = :id", id=mine["id"])
    await execute(
        conn,
        """update calendar_connections set status = 'active', calendar_id = :calendar, calendar_name = :name,
                  choices = '[]', next_pull_at = :now, last_synced_at = null,
                  sync_token = case when :same then sync_token end
           where id = :id""",
        id=mine["id"], calendar=calendar_id, name=choice["name"], now=rec.ctx.now,
        same=mine["calendar_id"] == calendar_id)
    rec.appended("calendar_connections", mine["id"])
    rec.lines.append(f"OK: {PROVIDERS[mine['provider']]} connected: {choice['name']}")


async def disconnect(rec: Recorder) -> dict[str, Any] | None:
    """Forget the speaker's connection. The events stay where they are on both sides. Returns the
    provider and the stored sign-in, so the caller can tell the provider it is finished with."""
    gone = await fetch_one(
        rec.ctx.conn, "delete from calendar_connections where member_id = :m and household_id = :h "
                      "returning id, provider, secret, status", m=rec.ctx.member_id, h=rec.ctx.household_id)
    if gone and gone["status"] != "choosing":   # backing out before picking a calendar changed nothing
        rec.appended("calendar_connections", gone["id"])
        rec.lines.append(f"OK: {PROVIDERS[gone['provider']]} disconnected")
    return gone


def seal(settings: Settings, **secret: str) -> str:
    assert settings.calendar_secret_key
    return crypto.seal(settings.calendar_secret_key, json.dumps(secret))


def unseal(settings: Settings, sealed: str) -> dict[str, str]:
    """Raises `crypto.Unreadable` when the key has changed since it was stored."""
    opened: dict[str, str] = json.loads(crypto.unseal(settings.calendar_secret_key or "", sealed))
    return opened


def provider_for(settings: Settings, connection: dict[str, Any]) -> Any:
    """The connected calendar as something to `async with`."""
    secret = unseal(settings, connection["secret"])
    if connection["provider"] == "apple":
        return apple.AppleCalendar(settings.apple_caldav_url, secret["username"], secret["password"],
                                   connection["calendar_id"], connection["timezone"])
    assert settings.google_client_id and settings.google_client_secret
    return google.GoogleCalendar(settings.google_client_id, settings.google_client_secret, secret["refresh_token"],
                                 connection["calendar_id"], connection["timezone"])


# ---------------------------------------------------------------- an event as both sides hold it
def entry_of(event: dict[str, Any]) -> Entry:
    """A household event as it is sent to a calendar. The title names who it is for, as the
    calendar link's feed does; an event with no end is given one, because a calendar needs it."""
    start, timezone = event["starts_at"], event["timezone"]
    all_day = bool(event["all_day"])   # null on an event put back by undo from a copy older than the column
    whole_day = day_bounds(local(start, timezone).date(), timezone)[1]
    return Entry(
        summary=calendar.label(event), start=start,
        end=event["ends_at"] or (whole_day if all_day else start + DEFAULT_LENGTH),
        all_day=all_day, rrule=canonical_rrule(event["rrule"]), exdates=tuple(sorted(event["exdates"])),
        location=event["location"], notes=event["notes"])


def _title(summary: str, event: dict[str, Any]) -> str:
    """The household title for a summary read from a calendar: without the "for Ada" that
    `entry_of` put there."""
    added = calendar.label(event).removeprefix(event["title"])
    if added and summary.lower().endswith(added.lower()) and len(summary) > len(added):
        return summary[:-len(added)]
    return summary


def _named(title: str, family: list[dict[str, Any]]) -> list[str]:
    """The family members a title names: "Tobi swimming" is Tobi's, so it is said to whoever looks
    after Tobi, as it would be had someone told the assistant."""
    return [person["id"] for person in family if re.search(rf"\b{re.escape(person['name'])}\b", title, re.IGNORECASE)]


def _still_to_come(entry: Entry, timezone: str, now: datetime) -> bool:
    if entry.rrule:
        return next_occurrence(entry.rrule, entry.start, timezone, now, entry.exdates) is not None
    return entry.end > now


# ---------------------------------------------------------------- reading: a calendar's changes applied here
async def _link(conn: AsyncConnection, connection_id: str, event_id: str | None, remote: Remote,
                synced: Entry) -> dict[str, Any]:
    """Note that `remote` is this event's copy on the calendar, as both sides now hold it."""
    link_id = await fetch_val(
        conn,
        """insert into calendar_links (connection_id, event_id, remote_id, remote_etag, synced)
           values (:c, :event, :remote, :etag, cast(:synced as jsonb))
           on conflict (connection_id, remote_id) do update
             set event_id = excluded.event_id, remote_etag = excluded.remote_etag, synced = excluded.synced
           returning id""",
        c=connection_id, event=event_id, remote=remote.id, etag=remote.etag, synced=jsonb(synced.json()))
    return {"id": link_id, "event_id": event_id, "remote_id": remote.id, "remote_etag": remote.etag,
            "synced": synced.json(), "instances": []}


async def _occurrence_changed(rec: Recorder, connection: dict[str, Any], series: dict[str, Any],
                              remote: Remote) -> None:
    """One date of a series was changed or deleted in the calendar: the series skips that date
    here, exactly as when one date is changed in chat. Both sides are then agreed that it is
    skipped, so it is not sent back as a change."""
    day = remote.original
    if day is None or day in series["instances"]:
        return
    event = await calendar.get_event(rec.ctx.conn, rec.ctx.household_id, series["event_id"])
    if event is None:
        return
    if event["status"] == "active" and event["rrule"] and day not in event["exdates"]:
        await calendar.modify_event(rec, event["id"], scope="all", exdates=[*event["exdates"], day])
        rec.lines.append(f"NOTE: {day:%a %-d %b} changed in {label(connection)}")
    base = Entry.of(series["synced"])
    agreed = replace(base, exdates=tuple(sorted({*base.exdates, day})))
    series["instances"] = [*series["instances"], day]
    series["synced"] = agreed.json()
    await execute(
        rec.ctx.conn, "update calendar_links set instances = cast(:days as date[]), synced = cast(:synced as jsonb) "
                      "where id = :id", days=series["instances"], synced=jsonb(agreed.json()), id=series["id"])


async def _apply(rec: Recorder, event: dict[str, Any], base: Entry, theirs: Entry) -> None:
    """Make the household event match what changed in the calendar since both sides last agreed.
    Only the changed details are touched, so a change made here to another detail survives."""
    changed = theirs.changed_from(base)
    wanted: dict[str, Any] = {}
    if "summary" in changed and (title := _title(theirs.summary, event)) != event["title"]:
        wanted["title"] = title
    if changed & {"start", "end", "all_day"}:
        if theirs.start != event["starts_at"]:
            wanted["starts_at"] = theirs.start
        if theirs.end != entry_of(event).end:
            wanted["ends_at"] = theirs.end
        if theirs.all_day != bool(event["all_day"]):
            wanted["all_day"] = theirs.all_day
    if "rrule" in changed and theirs.rrule != canonical_rrule(event["rrule"]):
        wanted["rrule"] = theirs.rrule or ""
    if "exdates" in changed:
        # Dates skipped or put back there, on top of whatever was skipped here in the meantime.
        skipped = (set(event["exdates"]) - (set(base.exdates) - set(theirs.exdates))) | set(theirs.exdates)
        if skipped != set(event["exdates"]):
            wanted["exdates"] = sorted(skipped)
    for name in ("location", "notes"):
        if name in changed and getattr(theirs, name) != event[name]:
            wanted[name] = getattr(theirs, name) or ""
    if wanted:
        await calendar.modify_event(rec, event["id"], scope="all", **wanted)


async def _take_one(rec: Recorder, connection: dict[str, Any], links: dict[str, dict[str, Any]],
                    family: list[dict[str, Any]], remote: Remote) -> None:
    """Apply one reported change. Raises ToolError when the calendar service will not have it."""
    conn, household_id, source = rec.ctx.conn, rec.ctx.household_id, label(connection)
    if remote.series_id:
        series = links.get(remote.series_id)
        if series is None or series["event_id"] is None:
            return   # an occurrence of a series that is not followed here
        await _occurrence_changed(rec, connection, series, remote)

    link = links.get(remote.id)
    if link is not None:
        if link["event_id"] is None:
            return   # the event is gone here and is about to be removed there
        event = await calendar.get_event(conn, household_id, link["event_id"])
        assert event is not None
        if remote.deleted:
            if event["status"] == "active":
                await calendar.modify_event(rec, event["id"], scope="all", cancel=True)
                rec.lines.append(f"NOTE: deleted in {source}")
            await execute(conn, "delete from calendar_links where id = :id", id=link["id"])
            del links[remote.id]
            return
        if remote.entry is None or (remote.etag and remote.etag == link["remote_etag"]):
            return   # it cannot be held here, or it is our own write coming back
        base = Entry.of(link["synced"])
        theirs = replace(remote.entry, exdates=tuple(sorted({*remote.entry.exdates, *link["instances"]})))
        if event["status"] == "active" and theirs != base:
            await _apply(rec, event, base, theirs)
            if rec.touched:
                rec.lines.append(f"NOTE: changed in {source}")
        await execute(conn, "update calendar_links set synced = cast(:synced as jsonb), remote_etag = :etag "
                            "where id = :id", synced=jsonb(theirs.json()), etag=remote.etag, id=link["id"])
        link["synced"], link["remote_etag"] = theirs.json(), remote.etag
        return

    entry = remote.entry
    if remote.deleted or entry is None:
        return
    if remote.owner and not remote.series_id and (mine := await _own(conn, household_id, remote.owner)):
        # An event this app made, back without a link: the answer to making it was lost, or the
        # calendar was connected before. If the event already has its copy there, this is a second
        # one and goes; if not, this is its copy. (A changed occurrence carries its series' mark
        # and is nobody's copy.)
        copied = any(other["event_id"] == mine["id"] for other in links.values())
        links[remote.id] = await _link(conn, connection["id"], None if copied else mine["id"], remote, entry)
        return
    if not _still_to_come(entry, connection["timezone"], rec.ctx.now):
        return   # over already: nothing to plan or remind about
    event_id = await calendar.schedule_event(
        rec, title=entry.summary, kind="activity" if entry.rrule else "appointment", starts_at=entry.start,
        ends_at=entry.end, rrule=entry.rrule, participant_ids=_named(entry.summary, family),
        location=entry.location, notes=entry.notes, all_day=entry.all_day, exdates=entry.exdates,
        allow_started=True)
    if rec.touched:
        rec.lines.append(f"NOTE: added in {source}")
    # Had it been said here too, it is the same plan, not a second one: `schedule_event` returned that one.
    links[remote.id] = await _link(conn, connection["id"], event_id, remote, entry)


async def _own(conn: AsyncConnection, household_id: str, owner: str) -> dict[str, Any] | None:
    try:
        UUID(owner)
    except ValueError:
        return None
    return await calendar.get_event(conn, household_id, owner)


async def take_in(conn: AsyncConnection, connection: dict[str, Any], remotes: list[Remote], now: datetime) -> int:
    """Apply a calendar's changes to the household, each as its own logged, undoable action in the
    name of the adult whose calendar it is. Returns how many changed something here."""
    ctx = Ctx(conn=conn, household_id=connection["household_id"], member_id=connection["member_id"],
              source="calendar", now=now)
    links = {row["remote_id"]: row for row in await fetch_all(
        conn, "select id, event_id, remote_id, remote_etag, synced, instances from calendar_links "
              "where connection_id = :c", c=connection["id"])}
    family = await members.family(conn, connection["household_id"], now)
    applied = 0
    for remote in remotes:
        if remote.skipped:
            log.info("calendar_event_skipped", connection_id=connection["id"], reason=remote.skipped)
        try:
            async with conn.begin_nested(), record(
                    ctx, "calendar.sync", {"calendar": label(connection), "event": remote.id}) as rec:
                await _take_one(rec, connection, links, family, remote)
            applied += bool(rec.touched)
        except ToolError as exc:
            # The calendar service would not have it (a repeat it cannot expand, say). It is left
            # out, and one event the household cannot hold does not stop the rest.
            log.info("calendar_event_refused", connection_id=connection["id"], reason=str(exc))
        except Exception:
            # A fault of ours on this one event. Its savepoint is rolled back and the rest are
            # still applied: stopping here would stop the calendar's every later change too.
            log.exception("calendar_event_failed", connection_id=connection["id"])
    return applied


# ---------------------------------------------------------------- writing: what differs here, sent there
@dataclass(frozen=True)
class Send:
    """One thing to do on the calendar, and the event as it will then stand on both sides."""
    do: str                           # create, update or delete
    event_id: str | None = None
    link_id: str | None = None
    remote_id: str | None = None
    entry: Entry | None = None        # the household's event now
    changed: frozenset[str] = frozenset()
    instances: tuple[Any, ...] = ()   # dates the calendar holds as changed occurrences: never sent as skipped


async def to_send(conn: AsyncConnection, connection: dict[str, Any], now: datetime) -> list[Send]:
    """Compare every followed event with what was last agreed and list what the calendar is missing."""
    events = {event["id"]: event for event in await calendar.active_events(conn, connection["household_id"])}
    links = await fetch_all(
        conn, "select id, event_id, remote_id, synced, instances from calendar_links where connection_id = :c "
              "order by id", c=connection["id"])
    work = []
    for link in links:
        event = events.get(link["event_id"])
        if event is None:   # cancelled here, or undone out of existence
            work.append(Send("delete", link_id=link["id"], remote_id=link["remote_id"]))
            continue
        mine = entry_of(event)
        if changed := mine.changed_from(Entry.of(link["synced"])):
            work.append(Send("update", event["id"], link["id"], link["remote_id"], mine, frozenset(changed),
                             tuple(link["instances"])))
    followed = {link["event_id"] for link in links}
    for event in events.values():
        mine = entry_of(event)
        if event["id"] not in followed and _still_to_come(mine, connection["timezone"], now):
            work.append(Send("create", event["id"], entry=mine))
    return work


async def _send(provider: Provider, conn: AsyncConnection, connection_id: str, job: Send) -> None:
    """Do one thing on the calendar and note it. Raises CalendarError when it could not be done.

    A change or a removal the calendar refuses for this one event is given up, not repeated: the
    person may not be the one who organises it. The copies then differ until it next changes
    there, and the calendar's version is then the one kept."""
    if job.do == "delete":
        assert job.remote_id
        try:
            await provider.delete(job.remote_id)
        except Refused as exc:
            log.warning("calendar_send_refused", connection_id=connection_id, do=job.do, reason=str(exc))
        await execute(conn, "delete from calendar_links where id = :id", id=job.link_id)
        return
    assert job.entry is not None
    sent = replace(job.entry, exdates=tuple(day for day in job.entry.exdates if day not in job.instances))
    if job.do == "create":
        assert job.event_id
        remote_id, etag = await provider.create(sent, job.event_id)
        await _link(conn, connection_id, job.event_id, Remote(remote_id, etag), job.entry)
        return
    assert job.remote_id
    etag = None
    try:
        etag = await provider.update(job.remote_id, sent, set(job.changed))
    except Gone:
        # Deleted there a moment ago. The next read says so and the event is cancelled here; until
        # then it is not sent again.
        pass
    except Refused as exc:
        log.warning("calendar_send_refused", connection_id=connection_id, do=job.do, reason=str(exc))
    await execute(conn, "update calendar_links set synced = cast(:synced as jsonb), "
                        "remote_etag = coalesce(:etag, remote_etag) where id = :id",
                  synced=jsonb(job.entry.json()), etag=etag, id=job.link_id)


# ---------------------------------------------------------------- one round
async def claim(now: datetime, limit: int = 20) -> list[dict[str, Any]]:
    """The active connections nobody is working on, each leased to this caller."""
    async with tx() as conn:
        return await fetch_all(
            conn,
            """update calendar_connections c set leased_until = :until
               from households h, members m
               where h.id = c.household_id and m.id = c.member_id and c.id in (
                   select id from calendar_connections
                   where status = 'active' and (leased_until is null or leased_until < :now)
                   order by next_pull_at nulls first for update skip locked limit :limit)
               returning c.*, h.timezone, m.name as member""",
            now=now, until=now + LEASE, limit=limit)


async def sync(connection: dict[str, Any], provider: Provider, now: datetime) -> dict[str, int]:
    """One round for one connection: read its changes when that is due, apply them, then send
    what differs here. Raises CalendarError; whatever was done before it stays done."""
    done = {"read": 0, "applied": 0, "sent": 0, "refused": 0}
    pulled = None
    if connection["next_pull_at"] is None or connection["next_pull_at"] <= now:
        async with tx() as conn:   # what is followed, for a calendar that says "gone" only by leaving it out
            followed = [link["remote_id"] for link in await fetch_all(
                conn, "select remote_id from calendar_links where connection_id = :c", c=connection["id"])]
        try:
            pulled = await provider.changes(connection["sync_token"], now, followed)
        except SyncReset:
            pulled = await provider.changes(None, now, followed)
        done["read"] = len(pulled[0])
    async with tx() as conn:
        await advisory_lock(conn, connection["household_id"])   # as a turn does: no two writers at once
        if pulled is not None:
            done["applied"] = await take_in(conn, connection, pulled[0], now)
            await execute(
                conn, "update calendar_connections set sync_token = :token, next_pull_at = :next where id = :id",
                token=pulled[1], next=now + PULL_EVERY, id=connection["id"])
            await execute(
                conn,
                """delete from calendar_links l using events e
                   where l.connection_id = :c and e.id = l.event_id and e.rrule is null
                     and coalesce(e.ends_at, e.starts_at) < :before""", c=connection["id"], before=now - KEEP_ENDED)
        work = await to_send(conn, connection, now)
    refused: CalendarError | None = None
    for job in work[:SEND_AT_ONCE]:
        try:
            async with tx() as conn:
                await _send(provider, conn, connection["id"], job)
            done["sent"] += 1
        except Refused as exc:   # a new event the calendar will not take; the others still go
            done["refused"] += 1
            refused = exc
            log.warning("calendar_send_refused", connection_id=connection["id"], do=job.do, reason=str(exc))
        except CalendarError:
            raise                # the whole calendar: a lost sign-in, a fault, a rate limit
        except Exception as exc:   # a fault of ours on this one event: the others still go
            done["refused"] += 1
            refused = Refused(type(exc).__name__)
            log.exception("calendar_send_failed", connection_id=connection["id"], do=job.do)
    if refused:
        raise refused            # tried again, but after a wait
    return done


async def finish(connection: dict[str, Any], now: datetime, error: Exception | None = None) -> bool:
    """Close a round: free the connection, or after a failure hold it back, longer each time.
    A lost sign-in, a lost key or a deleted calendar stops it for good and tells its owner once.
    Returns whether a message was queued."""
    async with tx() as conn:
        if error is None:
            await execute(conn, "update calendar_connections set leased_until = null, failures = 0, "
                                "last_error = null, last_synced_at = :now where id = :id", now=now, id=connection["id"])
            return False
        reason = f"{type(error).__name__}: {error}"[:200]
        if not isinstance(error, AuthLost | Gone | crypto.Unreadable):
            wait = min(FIRST_WAIT * 2 ** connection["failures"], MAX_WAIT)
            await execute(conn, "update calendar_connections set leased_until = :until, failures = failures + 1, "
                                "last_error = :reason where id = :id", until=now + wait, reason=reason,
                          id=connection["id"])
            log.warning("calendar_sync_failed", connection_id=connection["id"], reason=reason,
                        retry_in=int(wait.total_seconds()))
            return False
        await execute(conn, "update calendar_connections set status = 'broken', leased_until = null, "
                            "last_error = :reason where id = :id", reason=reason, id=connection["id"])
        log.warning("calendar_connection_broken", connection_id=connection["id"], reason=reason)
        return bool(await enqueue(conn, OutboundMessage(
            household_id=connection["household_id"], target="member", member_id=connection["member_id"],
            text=STOPPED.format(calendar=PROVIDERS[connection["provider"]])
            + STOPPED_MORE.get(connection["provider"], ""),
            dedupe_key=f"calendar_stopped:{connection['id']}:{now:%Y-%m-%d}"), send_after=now))


async def run(settings: Settings, now: datetime) -> int:
    """A round for every connection that is free. Returns how many messages were queued."""
    told = 0
    for connection in await claim(now):
        error: Exception | None = None
        try:
            async with provider_for(settings, connection) as provider:
                done = await sync(connection, provider, now)
            if any(done.values()):
                log.info("calendar_synced", connection_id=connection["id"], household_id=connection["household_id"],
                         **done)
        except (CalendarError, crypto.Unreadable) as exc:
            error = exc
        except Exception as exc:   # a fault of ours: it waits like any failure and does not stop the others
            log.exception("calendar_sync_crashed", connection_id=connection["id"])
            error = exc
        told += await finish(connection, now, error)
    return told

