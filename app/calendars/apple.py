"""Apple's iCloud Calendar, over CalDAV (RFC 4791) with collection sync (RFC 6578).

iCloud offers another app no OAuth and no push: a person signs in with the email of their Apple
Account and an app-specific password, and the worker asks what changed (ADR 0041).

A CalDAV calendar is a folder of iCalendar files, one for each event. A repeating event shares
its file with the dates of it that were changed by themselves: the series, and one more VEVENT
with a `RECURRENCE-ID` for each such date. Here the file is one `Remote` and each changed date is
another, with the file's address, a `#` and the date as its id. A date that was deleted is an
`EXDATE` on the series.

CalDAV says that something is gone only by leaving it out, so `changes` is given the ids the sync
follows and reports the ones that are no longer there.

None of this has been run against iCloud: no Apple account existed when it was written. The tests
run it against Radicale, a CalDAV server started for the test session, and against iCloud's
answers as other projects have published them (tests/contract/test_apple_calendar.py).
"""
import hashlib
import ipaddress
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import httpx
import icalendar

from app.calendars.base import (
    DEFAULT_LENGTH,
    NO_TITLE,
    AuthLost,
    CalendarError,
    Entry,
    Gone,
    Refused,
    Remote,
    RemoteCalendar,
    SyncReset,
    Unavailable,
    canonical_rrule,
)
from app.core.timeutil import day_bounds, local, normalise_rrule, utcnow

SERVER = "https://caldav.icloud.com"
PASSWORDS_PAGE = "https://account.apple.com"   # where a person makes an app-specific password
MARK = "houseagent-"         # an event this app made has a UID that begins so, then the household event's id
PRODID = "-//household-agent//EN"
TIMEOUT = 20.0
BATCH = 50                   # files asked for in one request
MAX_FILES = 10_000           # on a first read; a calendar with more is not read
MAX_PAGES = 40               # of one list of changes
MAX_HOPS = 3                 # redirects followed for one request
CANNOT_FOLLOW = "it repeats in a way that cannot be followed here"
HAS_GUESTS = "it has guests, and a change from here could send them an email"

D, C = "{DAV:}", "{urn:ietf:params:xml:ns:caldav}"
_NS = 'xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"'
ASK_PRINCIPAL = f"<d:propfind {_NS}><d:prop><d:current-user-principal/></d:prop></d:propfind>"
ASK_HOME = f"<d:propfind {_NS}><d:prop><c:calendar-home-set/></d:prop></d:propfind>"
ASK_CALENDARS = (f"<d:propfind {_NS}><d:prop><d:resourcetype/><d:displayname/>"
                 "<c:supported-calendar-component-set/><d:current-user-privilege-set/></d:prop></d:propfind>")
ASK_ETAG = f"<d:propfind {_NS}><d:prop><d:getetag/></d:prop></d:propfind>"
# The level is in the body and the Depth header is 0, as RFC 6578 3.2 defines the report.
ASK_CHANGES = (f"<d:sync-collection {_NS}><d:sync-token>{{token}}</d:sync-token><d:sync-level>1</d:sync-level>"
               "<d:prop><d:getetag/></d:prop></d:sync-collection>")
ASK_FILES = (f"<c:calendar-multiget {_NS}><d:prop><d:getetag/><c:calendar-data/></d:prop>{{hrefs}}"
             "</c:calendar-multiget>")
CAN_WRITE = {f"{D}write", f"{D}write-content", f"{D}all"}


# ---------------------------------------------------------------- addresses
def _path(href: str) -> str:
    """The path of an address a server wrote, in one spelling, so the same file always has the same id."""
    return quote(unquote(urlsplit(href).path), safe="/")


def _file(remote_id: str) -> str:
    """The file an id is in: a changed date's id is its file's, then `#` and the date."""
    return remote_id.partition("#")[0]


def _beside(url: str, server: str) -> bool:
    """Whether `url` is the configured server or a host beside it, over a connection no weaker.
    iCloud keeps each account on a host of its own (pNN-caldav.icloud.com) and says which in its
    answers; the password is sent nowhere else."""
    there, home = urlsplit(url), urlsplit(server)
    host, known = (there.hostname or "").lower(), (home.hostname or "").lower()
    if there.scheme not in ("https", home.scheme) or not host:
        return False
    if host == known:
        return True
    try:
        ipaddress.ip_address(known)
        return False                     # a numbered address has nothing beside it
    except ValueError:
        parent = known.partition(".")[2]
        return "." in parent and host.endswith("." + parent)


# ---------------------------------------------------------------- answers
@dataclass(frozen=True)
class _Answer:
    """One `response` of a multistatus: a file or folder, and the properties it was found to have."""
    href: str
    status: int | None               # of the thing itself: 404 when it is gone
    props: dict[str, ET.Element]


def _status(line: str | None) -> int | None:
    found = re.search(r"\b(\d{3})\b", line or "")
    return int(found[1]) if found else None


def _answers(body: bytes) -> tuple[list[_Answer], str | None]:
    """The responses of a multistatus, and the sync token if it carries one."""
    try:
        if b"<!DOCTYPE" in body or b"<!ENTITY" in body:     # a multistatus has neither; an entity can be made to grow
            raise ET.ParseError
        root = ET.fromstring(body)
    except ET.ParseError:
        raise Unavailable("an answer that could not be read") from None
    found = []
    for response in root.findall(f"{D}response"):
        props: dict[str, ET.Element] = {}
        for propstat in response.findall(f"{D}propstat"):
            if _status(propstat.findtext(f"{D}status")) == 200:
                props.update({prop.tag: prop for holder in propstat.findall(f"{D}prop") for prop in holder})
        found.append(_Answer((response.findtext(f"{D}href") or "").strip(),
                             _status(response.findtext(f"{D}status")), props))
    return found, (root.findtext(f"{D}sync-token") or "").strip() or None


def _raise(response: httpx.Response) -> None:
    """Sort a refusal into what the sync should do about it. Nothing of the request is kept in
    the error: it is stored and logged."""
    status = response.status_code
    if status == 401:
        raise AuthLost(str(status))
    if status in (404, 410):
        raise Gone(str(status))
    if status == 429 or (status >= 500 and status != 507):    # 507: the account has no room left
        raise Unavailable(str(status))
    raise Refused(str(status))


class _Dav:
    """HTTP to one CalDAV server with one sign-in."""

    def __init__(self, server: str, username: str, password: str, *, patient: bool = True) -> None:
        self._server = server
        self._patient = patient
        self._http = httpx.AsyncClient(timeout=TIMEOUT, auth=httpx.BasicAuth(username, password))

    async def close(self) -> None:
        await self._http.aclose()

    async def send(self, method: str, url: str, body: str | None = None,
                   headers: dict[str, str] | None = None) -> httpx.Response:
        """One request. For the worker a 401 is tried once more before the sign-in is called lost,
        because that stops the connection until its owner types a password again. A person at the
        form is told at once: Apple counts wrong passwords."""
        response = await self._once(method, url, body, headers)
        if response.status_code == 401 and self._patient:
            response = await self._once(method, url, body, headers)
        return response

    async def _once(self, method: str, url: str, body: str | None, headers: dict[str, str] | None) -> httpx.Response:
        kind = "text/calendar" if method == "PUT" else "application/xml"
        sent = {**({"Content-Type": f"{kind}; charset=utf-8"} if body is not None else {}), **(headers or {})}
        for _ in range(MAX_HOPS + 1):
            if not _beside(url, self._server):
                raise Refused("an address away from the calendar's own")
            try:
                response = await self._http.request(method, url, headers=sent,
                                                    content=None if body is None else body.encode())
            except httpx.HTTPError as exc:
                raise Unavailable(type(exc).__name__) from None
            if response.status_code not in (301, 302, 307, 308) or not response.headers.get("Location"):
                return response
            url = urljoin(url, response.headers["Location"])
        raise Unavailable("too many redirects")

    async def ask(self, url: str, body: str, depth: int = 0) -> list[_Answer]:
        """A PROPFIND."""
        response = await self.send("PROPFIND", url, body, {"Depth": str(depth)})
        if response.status_code != 207:
            _raise(response)
        return _answers(response.content)[0]

    async def where(self, url: str, body: str, tag: str) -> str:
        """The address a property points to. Whichever response has it is taken: iCloud is known to
        answer under another address than the one that was asked."""
        for answer in await self.ask(url, body):
            href = answer.props[tag].findtext(f"{D}href") if tag in answer.props else None
            if href and href.strip():
                return urljoin(url, href.strip())
        raise Refused("no calendars at that address")


# ---------------------------------------------------------------- signing in
async def calendars(server: str, username: str, password: str) -> list[RemoteCalendar]:
    """The calendars this sign-in can add events to, each with its address as its id. Raises
    AuthLost when the email and password are not accepted."""
    dav = _Dav(server, username, password, patient=False)
    try:
        principal = await dav.where(server.rstrip("/") + "/", ASK_PRINCIPAL, f"{D}current-user-principal")
        home = await dav.where(principal, ASK_HOME, f"{C}calendar-home-set")
        found = []
        for answer in await dav.ask(home, ASK_CALENDARS, depth=1):
            kinds = answer.props.get(f"{D}resourcetype")
            if kinds is None or kinds.find(f"{C}calendar") is None:
                continue                 # the folder itself, the inbox and outbox, a subscribed calendar
            holds = [comp.get("name", "").upper() for comp in answer.props.get(
                f"{C}supported-calendar-component-set", ET.Element("none")).iter(f"{C}comp")]
            may = answer.props.get(f"{D}current-user-privilege-set")
            if (holds and "VEVENT" not in holds) or (
                    may is not None and not CAN_WRITE & {right.tag for right in may.iter()}):
                continue                 # reminders only, or someone else's that can only be looked at
            address = urljoin(home, answer.href)
            name = " ".join((answer.props[f"{D}displayname"].text or "").split()
                            ) if f"{D}displayname" in answer.props else ""
            found.append(RemoteCalendar(address, name or unquote(_path(address).rstrip("/").rpartition("/")[2])))
        return found
    finally:
        await dav.close()


# ---------------------------------------------------------------- reading a file
def _instant(value: Any, timezone: str) -> tuple[datetime, bool]:
    """A start or an end as a UTC instant, and whether it is a whole day. A time with no zone is
    household time."""
    if not isinstance(value, datetime):
        return day_bounds(value, timezone)[0], True
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(timezone))
    return value.astimezone(UTC), False


def _day(value: Any, timezone: str) -> date:
    """The household-local date of an `EXDATE` or a `RECURRENCE-ID`."""
    if not isinstance(value, datetime):
        day: date = value
        return day
    return value.date() if value.tzinfo is None else local(value, timezone).date()


def _text(value: Any, joiner: str = "\n") -> str | None:
    lines = [" ".join(line.split()) for line in str(value or "").splitlines()]
    return joiner.join(line for line in lines if line) or None


def entry_of(event: Any, timezone: str, moved: Iterable[date] = ()) -> Entry:
    """A VEVENT as an entry. `moved` are the dates of a series that have a VEVENT of their own:
    the series skips them. Raises for an event that cannot be held here."""
    begins = event["DTSTART"].dt
    start, all_day = _instant(begins, timezone)
    end = None
    if event.get("DTEND") is not None:
        end = _instant(event["DTEND"].dt, timezone)[0]
    elif event.get("DURATION") is not None:
        end = _instant(begins + event["DURATION"].dt, timezone)[0]
    if end is None or end <= start:
        end = day_bounds(local(start, timezone).date(), timezone)[1] if all_day else start + DEFAULT_LENGTH
    rule = event.get("RRULE")
    if isinstance(rule, list) or event.get("RDATE") is not None or event.get("EXRULE") is not None:
        raise ValueError(CANNOT_FOLLOW)
    rrule = canonical_rrule(normalise_rrule(rule.to_ical().decode(), start, timezone)) if rule is not None else None
    skipped = {_day(value, timezone) for value in event.exdates} | set(moved) if rrule else set()
    return Entry(
        summary=_text(event.get("SUMMARY"), " ") or NO_TITLE, start=start, end=end, all_day=all_day, rrule=rrule,
        exdates=tuple(skipped), location=_text(event.get("LOCATION"), ", "), notes=_text(event.get("DESCRIPTION")))


def _opened(data: str, timezone: str) -> tuple[Any, Any, dict[date, Any]]:
    """A file as its calendar, the event itself (None if the file has none), and the VEVENTs of
    the dates changed by themselves. Raises ValueError for a file that cannot be followed."""
    try:
        calendar = icalendar.Calendar.from_ical(data)
        events = calendar.walk("VEVENT")
    except Exception:    # the library has several kinds of error for a file it cannot read
        raise ValueError("it could not be read") from None
    whole = [event for event in events if event.get("RECURRENCE-ID") is None]
    if not events:
        raise ValueError("it is not an event")
    if len(whole) != 1:
        raise ValueError("it is one date of a repeating event that is not on this calendar")
    moved: dict[date, Any] = {}
    for event in events:
        if event is not whole[0]:
            original: Any = event["RECURRENCE-ID"]
            if original.params.get("RANGE"):     # "this and every later one", which a skipped date cannot say
                raise ValueError(CANNOT_FOLLOW)
            moved[_day(original.dt, timezone)] = event
    return calendar, whole[0], moved


def _cancelled(event: Any) -> bool:
    return str(event.get("STATUS") or "").upper() == "CANCELLED"


def remotes_of(path: str, etag: str | None, data: str, timezone: str) -> list[Remote]:
    """One file as the sync reads it: the event, then each date of it that was changed by itself."""
    try:
        _, main, moved = _opened(data, timezone)
        uid = str(main.get("UID") or "")
        # Only the event itself is marked as ours. A changed date carries the same UID and is nobody's copy.
        owner = uid[len(MARK):len(MARK) + 36] if uid.startswith(MARK) else None
        if _cancelled(main):
            return [Remote(path, etag, deleted=True, owner=owner)]
        entry = entry_of(main, timezone, moved)
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return [Remote(path, etag, skipped=str(exc) if isinstance(exc, ValueError) and str(exc)
                       else "it could not be read")]
    found = [Remote(path, etag, entry=entry, owner=owner)]
    for day, event in sorted(moved.items()) if entry.rrule else ():
        part = f"{path}#{day:%Y%m%d}"
        try:
            found.append(Remote(part, etag, deleted=True) if _cancelled(event)
                         else Remote(part, etag, entry=entry_of(event, timezone)))
        except (ValueError, KeyError, TypeError, AttributeError):
            found.append(Remote(part, etag, skipped="it could not be read"))
    return found


# ---------------------------------------------------------------- writing a file
def _put(event: Any, name: str, value: Any) -> None:
    event.pop(name, None)
    if value is not None:
        event.add(name, value)


def _occurrence(event: Any, day: date, entry: Entry, timezone: str) -> Any:
    """The occurrence of a series on a household-local date, written the way the series' own start
    is: a date beside a date, and a time in the same zone beside a time."""
    anchor = event["DTSTART"].dt
    if not isinstance(anchor, datetime):
        return day
    at = datetime.combine(day, local(entry.start, timezone).time(), tzinfo=ZoneInfo(timezone))
    return at.replace(tzinfo=None) if anchor.tzinfo is None else at.astimezone(anchor.tzinfo)


def _write(event: Any, entry: Entry, timezone: str, changed: set[str] | None = None,
           held: Collection[date] = ()) -> None:
    """Put the `changed` details of an entry on a VEVENT (all of them when None). `held` are the
    dates the file keeps as VEVENTs of their own, which are not written as skipped as well."""
    every = changed is None
    changed = changed or set()
    if every or "summary" in changed:
        _put(event, "SUMMARY", entry.summary)
    if every or changed & {"start", "end", "all_day"}:
        event.pop("DURATION", None)
        if entry.all_day:    # a whole day is written as a date, and ends on the day after its last
            _put(event, "DTSTART", local(entry.start, timezone).date())
            _put(event, "DTEND", local(entry.end, timezone).date())
        else:
            _put(event, "DTSTART", local(entry.start, timezone))
            _put(event, "DTEND", local(entry.end, timezone))
    # The skipped dates are written at the series' own time of day, so they move with its start.
    if every or changed & {"rrule", "exdates", "all_day"} or ("start" in changed and entry.rrule):
        rule = icalendar.vRecur.from_ical(entry.rrule) if entry.rrule else None
        if rule is not None and entry.all_day and rule.get("UNTIL"):    # beside a date, UNTIL is a date too
            rule["UNTIL"] = [local(until, timezone).date() if isinstance(until, datetime) else until
                             for until in rule["UNTIL"]]
        skipped = [_occurrence(event, day, entry, timezone) for day in entry.exdates if day not in held]
        _put(event, "RRULE", rule)
        _put(event, "EXDATE", skipped if rule is not None and skipped else None)
    if every or "location" in changed:
        _put(event, "LOCATION", entry.location)
    if every or "notes" in changed:
        _put(event, "DESCRIPTION", entry.notes)


def _touch(event: Any, stamp: datetime) -> None:
    """Say that the event changed, as a calendar app does. iCloud is known to send `DTSTAMP` twice."""
    for name in ("DTSTAMP", "LAST-MODIFIED"):
        _put(event, name, stamp)
    number = event.get("SEQUENCE")
    _put(event, "SEQUENCE", (number if isinstance(number, int) else 0) + 1)


def _finished(calendar: Any) -> str:
    # A time is written with its zone's name, and RFC 5545 3.6.5 wants the zone defined in the file.
    calendar.add_missing_timezones()
    return str(calendar.to_ical().decode())


def new_file(uid: str, entry: Entry, timezone: str, stamp: datetime) -> str:
    """A new event as an iCalendar file. It carries no alarm: the assistant sends the reminders."""
    calendar, event = icalendar.Calendar(), icalendar.Event()
    calendar.add("PRODID", PRODID)
    calendar.add("VERSION", "2.0")
    event.add("UID", uid)
    event.add("DTSTAMP", stamp)
    event.add("CREATED", stamp)
    _write(event, entry, timezone)
    calendar.add_component(event)
    return _finished(calendar)


def has_guests(data: str) -> bool:
    """iCloud sends invitations, changes and cancellations by itself when an event with guests is
    written or deleted (RFC 6638). Nobody is emailed by this app, so such an event is read and
    never written."""
    try:
        return any(event.get("ATTENDEE") is not None for event in icalendar.Calendar.from_ical(data).walk("VEVENT"))
    except Exception:    # a file that cannot be read has no guests anyone can see
        return False


def _for_writing(data: str, timezone: str) -> tuple[Any, Any, dict[date, Any]]:
    if has_guests(data):
        raise Refused(HAS_GUESTS)
    try:
        return _opened(data, timezone)
    except ValueError as exc:
        raise Refused(str(exc)) from None


def changed_file(data: str, part: str, entry: Entry, changed: set[str], timezone: str, stamp: datetime) -> str:
    """A file with the `changed` details of its event made to match `entry`, or of one changed date
    when `part` names one. Everything else in it is kept: its alarms, and whatever Apple put there."""
    calendar, main, moved = _for_writing(data, timezone)
    if part:
        event = moved.get(datetime.strptime(part, "%Y%m%d").date())
        if event is None:
            raise Gone("that date is no longer changed by itself")
        if entry.rrule:
            raise Refused("one date of a repeating event cannot repeat by itself")
        _write(event, entry, timezone, changed - {"rrule", "exdates"})
    else:
        event = main
        _write(main, entry, timezone, changed, held=moved)
        if changed & {"start", "all_day"}:   # each changed date still names its date, at the series' new time
            for day, other in moved.items():
                _put(other, "RECURRENCE-ID", _occurrence(main, day, entry, timezone))
    _touch(event, stamp)
    return _finished(calendar)


def without_date(data: str, part: str, timezone: str, stamp: datetime) -> str:
    """A file without the VEVENT of one changed date, which the series now skips. Raises Gone when
    that date has none."""
    calendar, main, moved = _for_writing(data, timezone)
    event = moved.get(datetime.strptime(part, "%Y%m%d").date())
    if event is None:
        raise Gone("that date is no longer changed by itself")
    calendar.subcomponents.remove(event)
    main.add("EXDATE", [event["RECURRENCE-ID"].dt])    # the date, written as the series' own start is
    _touch(main, stamp)
    return _finished(calendar)


# ---------------------------------------------------------------- one calendar
class AppleCalendar:
    """One calendar of one signed-in account (`Provider`). Use it inside `async with`."""

    def __init__(self, server: str, username: str, password: str, calendar_url: str, timezone: str, *,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self._dav = _Dav(server, username, password)
        self._calendar = calendar_url.rstrip("/") + "/"
        self._folder = _path(self._calendar)
        self._timezone = timezone
        self._clock = clock          # only for the "last changed" stamps an iCalendar file carries

    async def __aenter__(self) -> "AppleCalendar":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._dav.close()

    def _url(self, path: str) -> str:
        return urljoin(self._calendar, path)

    def _uid(self, owner: str) -> str:
        """An event made from a household event has one UID on this calendar, so making it twice is
        not possible. The calendar's own address is part of it: one account cannot hold the same
        UID on two of its calendars."""
        return f"{MARK}{owner}-{hashlib.sha256(self._folder.encode()).hexdigest()[:8]}"

    async def _listing(self, sync_token: str | None) -> tuple[dict[str, str | None], set[str], str | None]:
        """The files that changed since the token, each with its etag, the ones that went, and the
        next token. With no token: every file there is."""
        listed: dict[str, str | None] = {}
        gone: set[str] = set()
        token = sync_token
        for _ in range(MAX_PAGES):
            response = await self._dav.send("REPORT", self._calendar, ASK_CHANGES.format(token=escape(token or "")),
                                            {"Depth": "0"})
            if response.status_code != 207:
                # RFC 6578 answers a token it no longer knows with 403 or 409 and `valid-sync-token`.
                # What iCloud answers is not known, so any refusal of a request that carried a token
                # is taken to mean "start again"; the read without one then says what is really wrong.
                if token and (b"valid-sync-token" in response.content or response.status_code in (400, 403, 409, 410)):
                    raise SyncReset
                _raise(response)
            answers, following = _answers(response.content)
            more = False
            for answer in answers:
                path = _path(answer.href)
                if path.rstrip("/") == self._folder.rstrip("/"):
                    # The folder itself: iCloud lists it as changed. On it, 507 means there is more to ask for.
                    more = more or answer.status == 507
                elif answer.status == 404:
                    gone.add(path)
                    listed.pop(path, None)
                else:
                    etag = answer.props.get(f"{D}getetag")
                    listed[path] = None if etag is None else etag.text
                    gone.discard(path)
            token = following or token
            if not more:
                return listed, gone, following
        raise Unavailable("too many changes")

    async def _files(self, paths: list[str]) -> tuple[dict[str, tuple[str | None, str]], set[str]]:
        """Each file's etag and iCalendar text, and the ones that are no longer there."""
        hrefs = "".join(f"<d:href>{escape(path)}</d:href>" for path in paths)
        response = await self._dav.send("REPORT", self._calendar, ASK_FILES.format(hrefs=hrefs))
        if response.status_code != 207:
            _raise(response)
        found: dict[str, tuple[str | None, str]] = {}
        missing: set[str] = set()
        for answer in _answers(response.content)[0]:
            path, data = _path(answer.href), answer.props.get(f"{C}calendar-data")
            if data is not None and data.text:
                etag = answer.props.get(f"{D}getetag")
                found[path] = (None if etag is None else etag.text, data.text)
            elif answer.status == 404:
                missing.add(path)
        for path in sorted(set(paths) - set(found) - missing):
            # Listed as changed, but not in the answer. It is asked for by itself, so that one file
            # the server will not hand over this way cannot hold every other change back.
            response = await self._dav.send("GET", self._url(path))
            if response.status_code == 200:
                found[path] = (response.headers.get("ETag"), response.text)
            elif response.status_code in (404, 410):
                missing.add(path)
            elif response.status_code in (401, 429) or response.status_code >= 500:
                _raise(response)         # the whole read is tried again
            else:
                found[path] = (None, "")     # left out, and logged as an event that could not be read
        return found, missing & set(paths)

    async def _one(self, path: str) -> tuple[str | None, str]:
        found, _ = await self._files([path])
        if path not in found:
            raise Gone("404")
        return found[path]

    async def changes(self, sync_token: str | None, since: datetime,
                      followed: Collection[str] = ()) -> tuple[list[Remote], str | None]:
        listed, gone, token = await self._listing(sync_token)
        if len(listed) > MAX_FILES:
            raise Unavailable("too many events")
        parts: dict[str, set[str]] = {}
        for remote_id in followed:
            parts.setdefault(_file(remote_id), set()).add(remote_id)
        if sync_token is None:       # every file there is was listed: one that is followed and not there has gone
            gone |= set(parts) - set(listed)
        found: list[Remote] = []
        paths = sorted(listed)
        for at in range(0, len(paths), BATCH):
            files, missing = await self._files(paths[at:at + BATCH])
            gone |= missing
            for path, (etag, data) in files.items():
                remotes = remotes_of(path, etag or listed.get(path), data, self._timezone)
                found += remotes
                if not remotes[0].skipped:
                    # A changed date that is followed and no longer has a VEVENT of its own was
                    # deleted there or put back into its series. Either way it is no longer a plan.
                    here = {remote.id for remote in remotes}
                    found += [Remote(part, deleted=True) for part in sorted(parts.get(path, set()) - here)]
        for path in sorted(gone):    # a file that went takes its changed dates with it
            found += [Remote(part, deleted=True) for part in sorted({path} | parts.get(path, set()))]
        if sync_token is None:
            found = [remote for remote in found
                     if remote.entry is None or remote.entry.rrule or remote.entry.end > since]
        return found, token

    async def _etag(self, path: str, response: httpx.Response) -> str | None:
        """The etag of a file just written. A server that changed what it was sent returns none
        with its answer (RFC 4791 5.3.4), and has to be asked."""
        if response.headers.get("ETag"):
            return str(response.headers["ETag"])
        try:
            for answer in await self._dav.ask(self._url(path), ASK_ETAG):
                if f"{D}getetag" in answer.props:
                    return answer.props[f"{D}getetag"].text
        except CalendarError:
            pass                     # it was written; without the etag its echo is told by its content
        return None

    async def create(self, entry: Entry, owner: str) -> tuple[str, str | None]:
        uid = self._uid(owner)
        path = f"{self._folder}{quote(uid, safe='')}.ics"
        body = new_file(uid, entry, self._timezone, self._clock())
        response = await self._dav.send("PUT", self._url(path), body, {"If-None-Match": "*"})
        if response.status_code == 412:
            # It is there already: the answer to making it was lost, or its link was. It is made to
            # match the household's event, which is what was being sent.
            response = await self._dav.send("PUT", self._url(path), body)
        if response.status_code not in (200, 201, 204):
            _raise(response)
        return path, await self._etag(path, response)

    async def _rewrite(self, path: str, change: Callable[[str], str]) -> str | None:
        """Read a file, change it and write it back, only if nobody else changed it in between."""
        for _ in range(2):
            etag, data = await self._one(path)
            response = await self._dav.send("PUT", self._url(path), change(data), {"If-Match": etag} if etag else None)
            if response.status_code != 412:
                break
        if response.status_code == 412:
            raise Unavailable("it kept changing")
        if response.status_code not in (200, 201, 204):
            _raise(response)
        return await self._etag(path, response)

    async def update(self, remote_id: str, entry: Entry, changed: set[str]) -> str | None:
        path, _, part = remote_id.partition("#")
        return await self._rewrite(path, lambda data: changed_file(
            data, part, entry, changed, self._timezone, self._clock()))

    async def delete(self, remote_id: str) -> None:
        path, _, part = remote_id.partition("#")
        try:
            if part:
                await self._rewrite(path, lambda data: without_date(data, part, self._timezone, self._clock()))
                return
            if has_guests((await self._one(path))[1]):
                raise Refused(HAS_GUESTS)
        except Gone:
            return
        response = await self._dav.send("DELETE", self._url(path))
        if response.status_code not in (200, 204, 404, 410):
            _raise(response)
