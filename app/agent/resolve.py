"""Item and location resolution (spec section 9.1): natural names in, rows out.

Reads happen here; creating an item or location, or learning an alias, goes through
app/services like every other write.
"""
import re
from dataclasses import dataclass
from typing import Any

import inflect
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db import fetch_all
from app.services import inventory

THRESHOLD = 0.55   # minimum trigram similarity for a fuzzy candidate
LEAD = 0.15        # the best candidate must beat the next by this much to win outright

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
