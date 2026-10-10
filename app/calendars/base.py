"""What a connected calendar is to the rest of the app (ADR 0038).

`Entry` is one event in the form both sides can hold, and `Provider` is the handful of calls the
sync needs from a calendar service. No module outside `app/calendars/` knows a provider's
payloads, as no module outside `app/channels/` knows a chat app's.
"""
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, timedelta
from typing import Any, Protocol

DEFAULT_LENGTH = timedelta(hours=1)   # a household event with no end is given this one on a calendar


@dataclass(frozen=True)
class Entry:
    """An event as it is exchanged. Times are UTC instants; an all-day event runs from midnight to
    midnight in the household's time zone. `exdates` are household-local dates."""
    summary: str
    start: datetime
    end: datetime
    all_day: bool = False
    rrule: str | None = None
    exdates: tuple[date, ...] = ()
    location: str | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        # One spelling of a rule and one order of dates, whoever built the entry, so that two
        # entries that mean the same compare equal.
        object.__setattr__(self, "rrule", canonical_rrule(self.rrule))
        object.__setattr__(self, "exdates", tuple(sorted(self.exdates)))

    def json(self) -> dict[str, Any]:
        return {**asdict(self), "start": self.start.isoformat(), "end": self.end.isoformat(),
                "exdates": [day.isoformat() for day in self.exdates]}

    @classmethod
    def of(cls, stored: dict[str, Any]) -> "Entry":
        return cls(**{**stored, "start": datetime.fromisoformat(stored["start"]),
                      "end": datetime.fromisoformat(stored["end"]),
                      "exdates": tuple(date.fromisoformat(day) for day in stored["exdates"])})

    def changed_from(self, other: "Entry") -> set[str]:
        """The names of the fields that differ."""
        return {field.name for field in fields(self) if getattr(self, field.name) != getattr(other, field.name)}


def canonical_rrule(rule: str | None) -> str | None:
    """A repeat rule with its parts in one order, so the same rule written two ways compares equal."""
    if not rule:
        return None
    parts = sorted(part for part in rule.strip().upper().removeprefix("RRULE:").split(";") if part)
    return ";".join(sorted(parts, key=lambda part: not part.startswith("FREQ=")))


@dataclass(frozen=True)
class Remote:
    """One change a calendar reports: an event as it now stands there, or that it is gone."""
    id: str
    etag: str | None = None
    deleted: bool = False
    entry: Entry | None = None        # None when it is gone, or cannot be held here (`skipped` says why)
    series_id: str | None = None      # set when this is one changed occurrence of a repeating event
    original: date | None = None      # that occurrence's own date, household-local
    owner: str | None = None          # the household event it was made from, if this app made it
    skipped: str | None = None


@dataclass(frozen=True)
class RemoteCalendar:
    id: str
    name: str


class CalendarError(Exception):
    """Something a provider refused or could not do."""


class AuthLost(CalendarError):
    """The sign-in no longer works: the person has to connect again."""


class SyncReset(CalendarError):
    """The provider has forgotten our place in its changes: read everything again."""


class Gone(CalendarError):
    """The event, or the whole calendar, is no longer there."""


class Refused(CalendarError):
    """The provider will not take this change, and asking again would not help."""


class Unavailable(CalendarError):
    """Not now: a timeout, a rate limit or a fault at the provider. Try later."""


class Provider(Protocol):
    """One connected calendar. Built per sync from the stored sign-in; holds no state between."""

    async def changes(self, sync_token: str | None, since: datetime) -> tuple[list[Remote], str | None]:
        """What changed since `sync_token`, and the token for next time. With no token: everything
        that ends after `since`. A repeating event comes before its changed occurrences."""
        ...

    async def create(self, entry: Entry, owner: str) -> tuple[str, str | None]:
        """Returns the new event's id and etag."""
        ...

    async def update(self, remote_id: str, entry: Entry, changed: set[str]) -> str | None:
        """Send only the `changed` fields. Returns the new etag."""
        ...

    async def delete(self, remote_id: str) -> None:
        """Gone already counts as done."""
        ...
