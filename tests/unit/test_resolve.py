"""Item and location resolution (spec section 9.1): exact, alias, fuzzy, ambiguous, new."""
import pytest

from app.agent.resolve import (
    Ambiguous,
    Candidate,
    Match,
    decide,
    match_item,
    normalise,
    resolve_item,
    resolve_location,
    variety_of,
)
from app.db import fetch_one, tx
from tests.helpers import add_item, location_id_of, seed_home


@pytest.mark.parametrize("raw,expected", [
    ("eggs", "egg"),
    ("  Eggs ", "Egg"),
    ("a dozen eggs", "egg"),
    ("2x milk", "milk"),
    ("the Scotch bonnets", "Scotch bonnet"),
    ("chicken thighs", "chicken thigh"),
    ("some tomatoes", "tomato"),
    ("oats", "oats"),            # exception list
    ("noodles", "noodles"),
    ("peas", "peas"),
    ("hummus", "hummus"),        # not a plural
    ("rice", "rice"),
    ("the", "the"),              # never strips down to nothing
])
def test_normalise(raw, expected):
    assert normalise(raw) == expected


def cand(name, score, exact=False, detail=None):
    return Candidate(name, name, exact, score, detail)


def test_decide_exact_beats_everything():
    assert decide([cand("Egg", 1.0, exact=True), cand("Eggplant", 0.6)]) == Match("Egg", "Egg")


def test_decide_single_fuzzy_candidate_is_learned():
    assert decide([cand("Indomie", 0.7), cand("Onion", 0.1)]) == Match("Indomie", "Indomie", learned=True)


def test_decide_clear_leader_wins():
    assert decide([cand("Tomato", 0.9), cand("Tomato paste", 0.6)]) == Match("Tomato", "Tomato", learned=True)


def test_decide_close_candidates_are_ambiguous():
    result = decide([cand("Bell pepper", 0.58, detail="fridge"), cand("Black pepper", 0.54, detail="store")])
    assert result == Ambiguous(["Bell pepper (fridge)", "Black pepper (store)"])


def test_decide_two_exact_matches_are_ambiguous():
    result = decide([cand("Scotch bonnet", 1.0, exact=True), cand("Black pepper", 1.0, exact=True)])
    assert isinstance(result, Ambiguous) and len(result.options) == 2


def test_decide_nothing_above_threshold_is_new():
    assert decide([cand("Almond milk", 0.42)]) is None
    assert decide([]) is None


async def test_exact_match_ignores_case_plural_and_articles():
    async with tx() as conn:
        home = await seed_home(conn)
        egg = await add_item(conn, home, "Egg", location="fridge")
        for said in ("eggs", "EGG", "a dozen eggs", "the egg"):
            found = await resolve_item(conn, home.id, said)
            assert isinstance(found, Match) and found.id == egg and not found.created


async def test_alias_match():
    async with tx() as conn:
        home = await seed_home(conn)
        bonnet = await add_item(conn, home, "Scotch bonnet", location="freezer", aliases=["ata rodo"])
        found = await resolve_item(conn, home.id, "Ata Rodo")
        assert isinstance(found, Match) and found.id == bonnet


async def test_fuzzy_match_learns_the_spelling_as_an_alias():
    async with tx() as conn:
        home = await seed_home(conn)
        item = await add_item(conn, home, "Chicken thigh", location="freezer")
        found = await resolve_item(conn, home.id, "chiken thighs")
        assert isinstance(found, Match) and found.id == item and found.learned
        row = await fetch_one(conn, "select aliases from items where id = :id", id=item)
        assert row["aliases"] == ["chiken thigh"]
        again = await match_item(conn, home.id, "chiken thighs")
        assert again == Match(item, "Chicken thigh")   # now an exact alias hit


async def test_close_candidates_are_ambiguous_and_nothing_is_created():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "Bell pepper", location="fridge")
        await add_item(conn, home, "Black pepper", location="store")
        found = await resolve_item(conn, home.id, "pepper")
        assert found == Ambiguous(["Bell pepper (fridge)", "Black pepper (store)"])
        assert await fetch_one(conn, "select 1 from items where lower(canonical_name) = 'pepper'") is None


async def test_unknown_item_is_created_in_store_or_the_given_location():
    async with tx() as conn:
        home = await seed_home(conn)
        freezer = await location_id_of(conn, home, "freezer")
        plain = await resolve_item(conn, home.id, "Indomie")
        frozen = await resolve_item(conn, home.id, "Scotch bonnets", freezer)
        assert isinstance(plain, Match) and plain.created and plain.name == "Indomie"
        assert isinstance(frozen, Match) and frozen.created and frozen.name == "Scotch bonnet"
        rows = {
            r["canonical_name"]: r["location"]
            for r in [await fetch_one(
                conn,
                "select i.canonical_name, l.name as location from items i "
                "join locations l on l.id = i.default_location_id where i.id = :id", id=m.id,
            ) for m in (plain, frozen)]
        }
        assert rows == {"Indomie": "store", "Scotch bonnet": "freezer"}


async def test_items_do_not_leak_across_households():
    async with tx() as conn:
        home = await seed_home(conn)
        other = await seed_home(conn, telegram_id=None)
        await add_item(conn, other, "Egg")
        found = await resolve_item(conn, home.id, "eggs")
        assert isinstance(found, Match) and found.created


async def test_locations_resolve_by_name_alias_and_spelling_then_get_created():
    async with tx() as conn:
        home = await seed_home(conn)
        ids = {name: await location_id_of(conn, home, name) for name in ("fridge", "freezer", "store")}
        assert (await resolve_location(conn, home.id, "Fridge")).id == ids["fridge"]
        assert (await resolve_location(conn, home.id, "deep freezer")).id == ids["freezer"]
        assert (await resolve_location(conn, home.id, "pantry")).id == ids["store"]
        assert (await resolve_location(conn, home.id, "cupboard")).id == ids["store"]
        assert (await resolve_location(conn, home.id, "frezer")).id == ids["freezer"]
        garage = await resolve_location(conn, home.id, "garage")
        assert isinstance(garage, Match) and garage.created and garage.id not in ids.values()


async def test_variety_of_names_the_household_item_a_new_name_ends_with():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "milk", location="fridge")
        await add_item(conn, home, "egg", location="fridge")
        await add_item(conn, home, "rice")
        await add_item(conn, home, "toilet roll", aliases=["loo roll"])
        await add_item(conn, home, "Bell pepper", location="fridge")

        async def of(raw):
            return await variety_of(conn, home.id, raw)

        assert await of("Semi Skimmed Milk") == ["milk (fridge)"]
        assert await of("a dozen free range eggs") == ["egg (fridge)"]      # normalised like any other name
        assert await of("quilted loo roll") == ["toilet roll (store)"]      # an alias counts
        assert await of("egg fried rice") == ["rice (store)"]               # the end of the name, not the start
        # Not a variety: the item itself, a spelling of it, a word that only contains it, the name in front.
        for raw in ("milk", "eggs", "bell peper", "price", "ice", "milkshake", "rice cake", "milk chocolate", "yam"):
            assert await of(raw) == [], raw
        assert await fetch_one(conn, "select 1 from items where canonical_name ilike '%skimmed%'") is None
