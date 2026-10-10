"""Google Calendar, through its REST API v3 and OAuth 2.0 for web server applications.

Everything here follows Google's documentation as read in October 2026 (ADR 0038 names the
pages). None of it has been run against Google: no credentials existed when it was written.
The tests replay responses built from the documented shapes.
"""
import re
import time
from collections.abc import Collection
from datetime import UTC, date, datetime
from datetime import time as clock
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx
import structlog

from app.calendars.base import (
    DEFAULT_LENGTH,
    NO_TITLE,
    AuthLost,
    Entry,
    Gone,
    Refused,
    Remote,
    RemoteCalendar,
    SyncReset,
    Unavailable,
    canonical_rrule,
)
from app.core.timeutil import local, normalise_rrule

log = structlog.get_logger()
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/calendar/v3"
# The least that does the job: see which calendars there are, and read and write events. Neither
# scope can change who a calendar is shared with, or delete one.
CALENDARS_SCOPE = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
SCOPES = (CALENDARS_SCOPE, EVENTS_SCOPE)
OWNER = "houseagent"       # the private property on an event this app made: the household event's id
TIMEOUT = 20.0
PAGE_SIZE = 250
MAX_PAGES = 40
SLOW_DOWN = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}
# Birthdays, focus time, out of office and working location are Google's own kinds of entry, not
# plans somebody made. An event Gmail made from an email (a flight, a booking) is a plan.
IMPORTED_TYPES = {"default", "fromGmail"}

_tokens: dict[str, tuple[str, float]] = {}   # refresh token -> (access token, when it stops working)


# ---------------------------------------------------------------- sign-in
def consent_url(client_id: str, redirect_uri: str, state: str) -> str:
    """Where the person is sent to say yes. `offline` and `consent` together make Google hand
    back a refresh token every time, also to someone who has connected before."""
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(SCOPES),
        "access_type": "offline", "prompt": "consent", "state": state})


async def _token(data: dict[str, str]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as http:
            response = await http.post(TOKEN_URL, data=data)
    except httpx.HTTPError as exc:
        raise Unavailable(type(exc).__name__) from None
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code == 200 and isinstance(body, dict):
        return body
    reason = str(body.get("error", response.status_code)) if isinstance(body, dict) else str(response.status_code)
    if reason == "invalid_grant":      # revoked, expired or used already
        raise AuthLost(reason)
    if response.status_code >= 500 or response.status_code == 429:
        raise Unavailable(reason)
    raise Refused(reason)              # invalid_client and the like: the installation's own settings are wrong


async def exchange(client_id: str, client_secret: str, redirect_uri: str, code: str) -> str:
    """Turn the code Google sent the person back with into a refresh token."""
    body = await _token({"code": code, "client_id": client_id, "client_secret": client_secret,
                         "redirect_uri": redirect_uri, "grant_type": "authorization_code"})
    granted = set(str(body.get("scope", "")).split())
    if not body.get("refresh_token") or not set(SCOPES) <= granted:
        raise Refused("not everything that was asked for was allowed")   # a box was left unticked
    _remember(body["refresh_token"], body)
    return str(body["refresh_token"])


def _remember(refresh_token: str, body: dict[str, Any]) -> str:
    _tokens[refresh_token] = (str(body["access_token"]), time.monotonic() + float(body.get("expires_in", 0)) - 60)
    return str(body["access_token"])


async def access_token(client_id: str, client_secret: str, refresh_token: str, *, fresh: bool = False) -> str:
    """An access token for this sign-in: the one in hand while it lasts, else a new one."""
    held = _tokens.get(refresh_token)
    if held and not fresh and held[1] > time.monotonic():
        return held[0]
    _tokens.pop(refresh_token, None)
    return _remember(refresh_token, await _token({
        "client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token,
        "grant_type": "refresh_token"}))


async def revoke(refresh_token: str) -> None:
    """Tell Google the sign-in is finished with. It is no loss if this fails: the token is forgotten here."""
    _tokens.pop(refresh_token, None)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as http:
            await http.post(REVOKE_URL, data={"token": refresh_token})
    except httpx.HTTPError as exc:
        log.warning("google_revoke_failed", error=type(exc).__name__)


# ---------------------------------------------------------------- reading an event
def _instant(value: dict[str, Any], timezone: str) -> tuple[datetime, bool]:
    """A start or an end as a UTC instant, and whether it is a whole day."""
    if value.get("date"):
        day = date.fromisoformat(value["date"])
        return datetime.combine(day, clock.min, tzinfo=ZoneInfo(timezone)).astimezone(UTC), True
    moment = datetime.fromisoformat(value["dateTime"])
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=ZoneInfo(value.get("timeZone") or timezone))
    return moment.astimezone(UTC), False


def _exdates(line: str, timezone: str) -> list[date]:
    """The household-local dates an `EXDATE` line leaves out."""
    params, _, values = line.partition(":")
    zone = re.search(r"TZID=([^;:]+)", params)
    found = []
    for value in filter(None, (value.strip() for value in values.split(","))):
        if "T" not in value:
            found.append(datetime.strptime(value, "%Y%m%d").date())
        elif value.endswith("Z"):
            found.append(local(datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC), timezone).date())
        else:
            there = datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=ZoneInfo(zone[1] if zone else timezone))
            found.append(local(there, timezone).date())
    return found


def _recurrence(lines: list[str], start: datetime, timezone: str) -> tuple[str | None, tuple[date, ...]]:
    """One `RRULE` and its `EXDATE`s. Raises ValueError for a repeat this app cannot follow."""
    kinds = [line.split(":", 1)[0].split(";", 1)[0].upper() for line in lines]
    if kinds.count("RRULE") > 1 or set(kinds) - {"RRULE", "EXDATE"}:
        raise ValueError("it repeats in a way that cannot be followed here")
    rule = next((line.split(":", 1)[1] for line, kind in zip(lines, kinds, strict=True) if kind == "RRULE"), None)
    skipped = {day for line, kind in zip(lines, kinds, strict=True) if kind == "EXDATE"
               for day in _exdates(line, timezone)}
    return (canonical_rrule(normalise_rrule(rule, start, timezone)) if rule else None), tuple(sorted(skipped))


def entry_of(item: dict[str, Any], timezone: str) -> Entry:
    start, all_day = _instant(item["start"], timezone)
    end = _instant(item["end"], timezone)[0] if item.get("end") and not item.get("endTimeUnspecified") else None
    rrule, exdates = _recurrence(item.get("recurrence") or [], start, timezone)
    return Entry(
        summary=" ".join((item.get("summary") or "").split()) or NO_TITLE, start=start,
        end=end if end and end > start else start + DEFAULT_LENGTH, all_day=all_day, rrule=rrule, exdates=exdates,
        location=(item.get("location") or "").strip() or None, notes=(item.get("description") or "").strip() or None)


def remote_of(item: dict[str, Any], timezone: str) -> Remote:
    """One item of an events list as the sync reads it."""
    series = item.get("recurringEventId")
    original = None
    if series and item.get("originalStartTime"):
        original = local(_instant(item["originalStartTime"], timezone)[0], timezone).date()
    common: dict[str, Any] = {
        "id": item["id"], "etag": item.get("etag"), "series_id": series, "original": original,
        "owner": (item.get("extendedProperties") or {}).get("private", {}).get(OWNER)}
    if item.get("status") == "cancelled":
        return Remote(deleted=True, **common)
    if item.get("eventType", "default") not in IMPORTED_TYPES:
        return Remote(skipped="it is not an ordinary event", **common)
    try:
        return Remote(entry=entry_of(item, timezone), **common)
    except (KeyError, ValueError) as exc:
        return Remote(skipped=str(exc) or "it could not be read", **common)


# ---------------------------------------------------------------- writing an event
def _when(moment: datetime, all_day: bool, timezone: str) -> dict[str, Any]:
    """Both forms are always named, the unused one as null, so a change between a timed event and
    a whole day clears the other."""
    here = local(moment, timezone)
    if all_day:
        return {"date": here.date().isoformat(), "dateTime": None, "timeZone": None}
    return {"date": None, "dateTime": here.isoformat(), "timeZone": timezone}


def _recurrence_lines(entry: Entry, timezone: str) -> list[str] | None:
    if not entry.rrule:
        return None
    rule, at = entry.rrule, local(entry.start, timezone)
    if entry.all_day:   # beside a date, UNTIL is a date too (RFC 5545 3.3.10)
        rule = re.sub(r"UNTIL=(\d{8}T\d{6}Z)", lambda until: "UNTIL=" + local(
            datetime.strptime(until[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC), timezone).strftime("%Y%m%d"), rule)
    lines = [f"RRULE:{rule}"]
    if entry.exdates and entry.all_day:
        lines.append("EXDATE;VALUE=DATE:" + ",".join(f"{day:%Y%m%d}" for day in entry.exdates))
    elif entry.exdates:
        lines.append(f"EXDATE;TZID={timezone}:" + ",".join(f"{day:%Y%m%d}T{at:%H%M%S}" for day in entry.exdates))
    return lines


def body_of(entry: Entry, timezone: str, changed: set[str] | None = None) -> dict[str, Any]:
    """The fields of an event resource for `changed` (all of them when None). A null clears."""
    every = changed is None
    changed = changed or set()
    body: dict[str, Any] = {}
    if every or "summary" in changed:
        body["summary"] = entry.summary
    if every or changed & {"start", "end", "all_day"}:
        body["start"] = _when(entry.start, entry.all_day, timezone)
        body["end"] = _when(entry.end, entry.all_day, timezone)
    # The skipped dates are written at the series' own time of day, so they move with its start.
    if every or changed & {"rrule", "exdates", "all_day"} or ("start" in changed and entry.rrule):
        body["recurrence"] = _recurrence_lines(entry, timezone)
    if every or "location" in changed:
        body["location"] = entry.location
    if every or "notes" in changed:
        body["description"] = entry.notes
    return body


def _without_nulls(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _without_nulls(item) for key, item in value.items() if item is not None}
    return value


def _reason(response: httpx.Response) -> str:
    try:
        error = response.json()["error"]
        return str((error.get("errors") or [{}])[0].get("reason") or error.get("status") or response.status_code)
    except (ValueError, KeyError, TypeError, AttributeError, IndexError):
        return str(response.status_code)


def _raise(response: httpx.Response) -> None:
    """Sort a refusal into what the sync should do about it."""
    reason, status = _reason(response), response.status_code
    if status == 401:
        raise AuthLost(reason)
    if status in (404, 410):
        raise Gone(reason)
    if status == 429 or status >= 500 or (status == 403 and reason in SLOW_DOWN):
        raise Unavailable(reason)
    raise Refused(reason)


async def calendars(token: str) -> tuple[str | None, list[RemoteCalendar]]:
    """The account's address, and the calendars the person can write to, their main one first."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as http:
            response = await http.get(f"{API}/users/me/calendarList", headers={"Authorization": f"Bearer {token}"},
                                      params={"minAccessRole": "writer", "maxResults": PAGE_SIZE})
    except httpx.HTTPError as exc:
        raise Unavailable(type(exc).__name__) from None
    if response.status_code != 200:
        _raise(response)
    items = [item for item in response.json().get("items", []) if not item.get("deleted")]
    account = next((item["id"] for item in items if item.get("primary")), None)
    found = [RemoteCalendar(item["id"], item.get("summaryOverride") or item.get("summary") or item["id"])
             for item in sorted(items, key=lambda item: not item.get("primary"))]
    return account, found


class GoogleCalendar:
    """One calendar of one signed-in account (`Provider`). Use it inside `async with`."""

    def __init__(self, client_id: str, client_secret: str, refresh_token: str, calendar_id: str,
                 timezone: str) -> None:
        self._client = (client_id, client_secret, refresh_token)
        self._events = f"{API}/calendars/{quote(calendar_id, safe='')}/events"
        self._timezone = timezone
        self._http = httpx.AsyncClient(timeout=TIMEOUT)

    async def __aenter__(self) -> "GoogleCalendar":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._http.aclose()

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """One call with the access token. A 401 is tried once more with a new token before the
        sign-in is called lost: an access token can be withdrawn before its hour is up."""
        response = None
        for fresh in (False, True):
            token = await access_token(*self._client, fresh=fresh)
            try:
                response = await self._http.request(method, url, headers={"Authorization": f"Bearer {token}"},
                                                    **kwargs)
            except httpx.HTTPError as exc:
                raise Unavailable(type(exc).__name__) from None
            if response.status_code != 401:
                break
        assert response is not None
        return response

    async def changes(self, sync_token: str | None, since: datetime,
                      followed: Collection[str] = ()) -> tuple[list[Remote], str | None]:
        # Google names what was deleted, so `followed` is not needed. Deleted events are asked for
        # on the first read too: after a reset it is how a deletion made in the meantime is seen.
        # Nothing but `syncToken` may differ between reads.
        params: dict[str, Any] = {"maxResults": PAGE_SIZE, "showDeleted": "true", "singleEvents": "false"}
        if sync_token:
            params["syncToken"] = sync_token
        else:
            params["timeMin"] = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        items: list[dict[str, Any]] = []
        for _ in range(MAX_PAGES):
            response = await self._send("GET", self._events, params=params)
            if response.status_code == 410:
                raise SyncReset
            if response.status_code != 200:
                _raise(response)
            page = response.json()
            items += page.get("items", [])
            if not page.get("nextPageToken"):
                found = [remote_of(item, self._timezone) for item in items]
                return sorted(found, key=lambda remote: remote.series_id is not None), page.get("nextSyncToken")
            params["pageToken"] = page["nextPageToken"]
        raise Unavailable("too many pages")

    async def create(self, entry: Entry, owner: str) -> tuple[str, str | None]:
        body = _without_nulls(body_of(entry, self._timezone))
        # The assistant reminds the household, so Google is told not to as well; and the event is
        # marked as ours, so it is known again if the answer to this call is lost.
        body |= {"reminders": {"useDefault": False, "overrides": []},
                 "extendedProperties": {"private": {OWNER: owner}}}
        response = await self._send("POST", self._events, params={"sendUpdates": "none"}, json=body)
        if response.status_code != 200:
            _raise(response)
        made = response.json()
        return str(made["id"]), made.get("etag")

    async def update(self, remote_id: str, entry: Entry, changed: set[str]) -> str | None:
        response = await self._send("PATCH", f"{self._events}/{quote(remote_id, safe='')}",
                                    params={"sendUpdates": "none"}, json=body_of(entry, self._timezone, changed))
        if response.status_code != 200:
            _raise(response)
        etag: str | None = response.json().get("etag")
        return etag

    async def delete(self, remote_id: str) -> None:
        response = await self._send("DELETE", f"{self._events}/{quote(remote_id, safe='')}",
                                    params={"sendUpdates": "none"})
        if response.status_code not in (200, 204, 404, 410):
            _raise(response)
