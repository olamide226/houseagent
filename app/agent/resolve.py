"""Item, location, member and event resolution (spec sections 9.1 and 9.2): natural names in, rows out.

Reads happen here; creating an item or location, or learning an alias, goes through
app/services like every other write.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import inflect
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.timeutil import next_occurrence
from app.db import fetch_all
from app.services import inventory

THRESHOLD = 0.55   # minimum trigram similarity for a fuzzy candidate
LEAD = 0.15        # the best candidate must beat the next by this much to win outright
EVENT_THRESHOLD = 0.3
EVENT_WINDOW = timedelta(days=60)

_LEADING = {"a", "an", "the", "some", "of", "dozen", "half", "couple", "few"}
_COUNT = re.compile(r"^(\d+([.,]\d+)?x?|x\d+)$")
# Words inflect would mangle, or that are only ever said in the plural.
_KEEP_PLURAL = {"oats", "noodles", "peas", "beans", "lentils", "greens", "grits", "crisps", "chips", "molasses"}
_inflect = inflect.engine()


def normalise(raw: str) -> str:
    """Trim, drop leading articles and quantities, singularise the last word. Keeps casing."""
    words = raw.split()
    while len(words) > 1 and (words[0].lower() in _LEADING or _COUNT.fullmatch(words[0].lower())):
        words.pop(0)
    if not words:
        return ""
    last = words[-1]
    lower = last.lower()
    if len(lower) > 3 and lower not in _KEEP_PLURAL and not lower.endswith(("ss", "us", "is", "as")):
        singular = _inflect.singular_noun(lower)
        if singular and singular != lower:
            words[-1] = singular if last.islower() else singular.capitalize()
    return " ".join(words)


@dataclass(frozen=True)
class Candidate:
    id: str
    name: str
    exact: bool
    score: float
    detail: str | None = None   # shown in ambiguity options, e.g. the item's usual location

    @property
    def label(self) -> str:
        return f"{self.name} ({self.detail})" if self.detail else self.name


@dataclass(frozen=True)
class Match:
    id: str
    name: str
    learned: bool = False   # fuzzy hit: the input should become an alias
    created: bool = False


@dataclass(frozen=True)
class Ambiguous:
    options: list[str]


def decide(candidates: list[Candidate]) -> Match | Ambiguous | None:
    """Steps 2 to 5 over scored candidates: exact hit, fuzzy hit, ambiguity, or nothing."""
    exact = [c for c in candidates if c.exact]
    if len(exact) == 1:
        return Match(exact[0].id, exact[0].name)
    if exact:
        return Ambiguous([c.label for c in exact])
    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
    if not ranked or ranked[0].score < THRESHOLD:
        return None
    best = ranked[0]
    close = [c for c in ranked[1:] if best.score - c.score < LEAD]
    if close:
        return Ambiguous([c.label for c in [best, *close]])
    return Match(best.id, best.name, learned=True)


async def _candidates(conn: AsyncConnection, sql: str, household_id: str, key: str) -> list[Candidate]:
    rows: list[dict[str, Any]] = await fetch_all(conn, sql, household=household_id, key=key)
    return [Candidate(r["id"], r["name"], r["exact"], float(r["score"]), r["detail"]) for r in rows]


_ITEMS = """
select i.id, i.canonical_name as name, l.name as detail,
       (lower(i.canonical_name) = :key or :key = any(i.aliases)) as exact,
       greatest(similarity(lower(i.canonical_name), :key),
                coalesce((select max(similarity(a, :key)) from unnest(i.aliases) a), 0)) as score
from items i left join locations l on l.id = i.default_location_id
where i.household_id = :household
order by exact desc, score desc limit 8"""

_LOCATIONS = """
select l.id, l.name, null as detail,
       (lower(l.name) = :key or :key = any(l.aliases)) as exact,
       greatest(similarity(lower(l.name), :key),
                coalesce((select max(similarity(a, :key)) from unnest(l.aliases) a), 0)) as score
from locations l
where l.household_id = :household
order by exact desc, score desc limit 8"""


async def match_item(conn: AsyncConnection, household_id: str, raw: str) -> Match | Ambiguous | None:
    """Read-only lookup, for queries that must not create anything."""
    return decide(await _candidates(conn, _ITEMS, household_id, normalise(raw).lower()))


async def match_location(conn: AsyncConnection, household_id: str, raw: str) -> Match | Ambiguous | None:
    return decide(await _candidates(conn, _LOCATIONS, household_id, " ".join(raw.lower().split())))


async def resolve_item(
    conn: AsyncConnection, household_id: str, raw: str, location_id: str | None = None
) -> Match | Ambiguous:
    """Find the item, learning a fuzzy spelling as an alias, or create it (default location: store)."""
    name = normalise(raw)
    found = await match_item(conn, household_id, raw)
    if found is None:
        item_id = await inventory.create_item(conn, household_id, name, location_id)
        return Match(item_id, name, created=True)
    if isinstance(found, Match) and found.learned:
        await inventory.add_item_alias(conn, found.id, name.lower())
    return found


async def resolve_location(conn: AsyncConnection, household_id: str, raw: str) -> Match | Ambiguous:
    name = " ".join(raw.lower().split())
    found = await match_location(conn, household_id, raw)
    if found is None:
        return Match(await inventory.create_location(conn, household_id, name), name, created=True)
    if isinstance(found, Match) and found.learned:
        await inventory.add_location_alias(conn, found.id, name)
    return found


# ---------------------------------------------------------------- members
_MEMBERS = """
select m.id, m.name, m.role as detail, lower(m.name) = :key as exact, similarity(lower(m.name), :key) as score
from members m
where m.household_id = :household
order by exact desc, score desc limit 8"""

_ME = {"me", "i", "myself"}
_ADULTS = {"us", "we", "both of us"}
_CHILDREN = {"the kids", "kids", "the children", "children"}


async def resolve_members(conn: AsyncConnection, household_id: str, names: list[str],
                          me: str | None) -> tuple[list[str], list[str]]:
    """Member ids for the people named, in order, and the names that matched nobody.

    "me" is the speaker, "us" every adult, "the kids" every child; anything else is a name."""
    family = await fetch_all(conn, "select id, role from members where household_id = :h order by created_at",
                             h=household_id)
    found: list[str] = []
    unknown: list[str] = []
    for raw in names:
        key = " ".join(raw.lower().split())
        if key in _ME:
            ids = [me] if me else []
        elif key in _ADULTS:
            ids = [member["id"] for member in family if member["role"] == "adult"]
        elif key in _CHILDREN:
            ids = [member["id"] for member in family if member["role"] == "child"]
        else:
            match = decide(await _candidates(conn, _MEMBERS, household_id, key))
            ids = [match.id] if isinstance(match, Match) else []
        if not ids:
            unknown.append(raw)
        found += [member_id for member_id in ids if member_id not in found]
    return found, unknown


# ---------------------------------------------------------------- events
@dataclass(frozen=True)
class EventCandidate:
    id: str
    title: str
    next_start: datetime
    score: float


_EVENTS = """
select e.id, e.title, e.starts_at, e.rrule, e.exdates, h.timezone,
       greatest(similarity(lower(e.title || ' ' || p.names), :key),
                word_similarity(lower(e.title), :key),
                word_similarity(:key, lower(e.title || ' ' || p.names))) as score
from events e join households h on h.id = e.household_id,
     lateral (select coalesce(string_agg(m.name, ' '), '') as names
              from members m where m.id = any(e.participant_ids)) p
where e.household_id = :household and e.status = 'active'"""


async def rank_events(conn: AsyncConnection, household_id: str, raw: str, now: datetime) -> list[EventCandidate]:
    """Active events whose next occurrence is within 60 days, best match on title and
    participant names first, ties broken by soonest."""
    rows = await fetch_all(conn, _EVENTS, household=household_id, key=" ".join(raw.lower().split()))
    ranked = []
    for row in rows:
        upcoming = (next_occurrence(row["rrule"], row["starts_at"], row["timezone"], now, row["exdates"])
                    if row["rrule"] else row["starts_at"])
        if upcoming is not None and now <= upcoming <= now + EVENT_WINDOW:
            ranked.append(EventCandidate(row["id"], row["title"], upcoming, round(float(row["score"]), 2)))
    return sorted(ranked, key=lambda candidate: (-candidate.score, candidate.next_start))
