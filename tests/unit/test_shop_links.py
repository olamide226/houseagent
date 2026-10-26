"""A shop link that is lost, dead or made for an old address (ADR 0042): the word shops offers a
new one and a yes makes it, a change of the app's address is told to each adult once, and a link
copied for a shop is a ping when a hand-made shortcut asks for it with GET.
"""
import asyncio
import importlib.util
import re
from datetime import timedelta
from pathlib import Path

import pytest

from app.agent.loop import LoopRuntime
from app.config import get_settings
from app.core.envelope import Channel
from app.core.identity import hash_token
from app.db import execute, fetch_all, fetch_val, tx
from app.pipeline import inbound, router
from app.services import households, members
from app.worker import jobs
from tests.helpers import FakeAdapter, FakeLLM, add_member, london, say, technical_words, tg_update
from tests.unit.test_presence import TESCO_LIST, household, on_list, pings, words_of

SECRET = {"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"}
URL = re.compile(r"http://testserver/presence/([\w-]{43})")
OLD = "https://radius-marked-example.trycloudflare.test"      # where the app answered before


async def post(client, update):
    assert (await client.post("/webhooks/telegram", json=update, headers=SECRET)).status_code == 200


async def rows(sql, **params):
    async with tx() as conn:
        return await fetch_all(conn, sql, **params)


async def hashes():
    return {r["name"]: r["presence_token_hash"] for r in await rows("select name, presence_token_hash from members")}


async def sent_to(name):
    """Texts queued for one person, oldest first."""
    return [r["text"] for r in await rows("select o.text from outbox o join members m on m.id = o.member_id "
                                          "where m.name = :n order by o.created_at", n=name)]


async def turn(home, *script):
    """Process what is waiting; the model answers anything that reaches it with `script`."""
    return await inbound.process_household(home.id, LoopRuntime(FakeLLM(*script)), {})


async def made_for(base, *names):
    async with tx() as conn:
        await execute(conn, "update members set presence_link_base = :base where name = any(:names)",
                      base=base, names=list(names))


# ---------------------------------------------------------------- the word shops, with a link already
async def test_shops_from_someone_with_a_link_asks_first_and_a_yes_replaces_it_so_the_old_one_stops(client):
    async with tx() as conn:
        home, tokens = await household(conn)
        await on_list(conn, home, "bleach", shop="Tesco")
    before = await hashes()

    await post(client, tg_update(1, "shops", user_id=1002, name="Ada"))
    assert await turn(home) == 0                                             # answered by code, not the model
    assert await sent_to("Ada") == [households.SHOPS_RENEW_OFFER]
    assert await hashes() == before                                          # nothing is replaced yet
    offer = households.SHOPS_RENEW_OFFER
    assert "Would you like a new one?" in offer and "would stop working" in offer and "Reply yes" in offer
    assert technical_words(offer) == []

    await post(client, tg_update(2, "Yes please!", user_id=1002, name="Ada"))
    assert await turn(home) == 0
    renewed = (await sent_to("Ada"))[-1]
    token = URL.search(renewed).group(1)
    assert renewed == households.presence_text(f"http://testserver/presence/{token}", replaced=True)
    assert "Your old one, and any shortcut you made with it, no longer work" in renewed
    assert "walks you through it again" in renewed and technical_words(renewed.replace(URL.search(renewed)[0], "")) == []
    after = await hashes()
    assert after["Ada"] == hash_token(token) != before["Ada"] and after["Ola"] == before["Ola"]
    assert token not in after.values()                                       # still only the hash is kept
    (row,) = await rows("select presence_link_base, presence_renew_offered_at from members where name = 'Ada'")
    assert row == {"presence_link_base": "http://testserver", "presence_renew_offered_at": None}

    # The old link and anything made from it stop working; the new one works.
    await client.post(f"/presence/{tokens['Ada']}/enter/Tesco%20Extra")
    assert await pings() == []
    assert (await client.get(f"/presence/{tokens['Ada']}")).status_code == 404
    await client.post(f"/presence/{token}/enter/Tesco%20Extra")
    assert await pings() == [("Ada", "Tesco Extra", "enter")]
    assert (await sent_to("Ada"))[-1].startswith("You're at Tesco Extra")

    await post(client, tg_update(3, "yes", user_id=1002, name="Ada"))     # a second yes is the model's
    assert await turn(home, say("Glad it's sorted.")) == 1
    assert (await hashes())["Ada"] == after["Ada"]


@pytest.mark.parametrize("answer", ["no thanks", "what would that do?"])
async def test_anything_but_a_yes_goes_to_the_model_and_a_later_yes_replaces_nothing(client, answer):
    async with tx() as conn:
        home, _ = await household(conn)
    before = await hashes()
    await post(client, tg_update(1, "shops", user_id=1002, name="Ada"))
    await turn(home)
    await post(client, tg_update(2, answer, user_id=1002, name="Ada"))
    assert await turn(home, say("OK, I've left your link as it is.")) == 1
    await post(client, tg_update(3, "yes", user_id=1002, name="Ada"))     # to something else by now
    assert await turn(home, say("Great.")) == 1
    assert await hashes() == before and len(await sent_to("Ada")) == 1


async def test_a_yes_more_than_an_hour_later_replaces_nothing_and_shops_again_asks_again(client):
    async with tx() as conn:
        home, _ = await household(conn)
    before = await hashes()
    await post(client, tg_update(1, "shops", user_id=1002, name="Ada"))
    await turn(home)
    async with tx() as conn:
        await execute(conn, "update members set presence_renew_offered_at = presence_renew_offered_at - cast(:late as interval)",
                      late=households.RENEW_WINDOW + timedelta(seconds=1))
    await post(client, tg_update(2, "yes", user_id=1002, name="Ada"))
    assert await turn(home, say("Send me the word shops again for a new link.")) == 1
    assert await hashes() == before

    await post(client, tg_update(3, "shops", user_id=1002, name="Ada"))
    await post(client, tg_update(4, "ok", user_id=1002, name="Ada"))      # in the same moment: before the question
    assert await turn(home, say("Here you go.")) == 1
    await post(client, tg_update(5, "ok", user_id=1002, name="Ada"))
    assert await turn(home) == 0
    assert (await hashes())["Ada"] != before["Ada"]


async def test_a_yes_in_the_family_group_or_from_someone_else_is_not_the_answer(client):
    async with tx() as conn:
        home, _ = await household(conn)
    before = await hashes()
    await post(client, tg_update(1, "shops", user_id=1002, name="Ada"))
    await turn(home)
    await post(client, tg_update(2, "yes", user_id=1002, name="Ada", chat_id=-500, chat_type="group"))
    await post(client, tg_update(3, "yes", user_id=1001, name="Ola"))
    assert await turn(home, say("OK"), say("OK")) == 2
    assert await hashes() == before

    await post(client, tg_update(4, "yes", user_id=1002, name="Ada"))     # her own chat: still the answer
    assert await turn(home) == 0
    assert (await hashes())["Ada"] != before["Ada"]


async def test_a_thumbs_up_on_the_question_is_a_yes(client):
    async with tx() as conn:
        home, _ = await household(conn)
    before = await hashes()
    await post(client, tg_update(1, "shops", user_id=1002, name="Ada"))
    await turn(home)
    await router.dispatch_due({Channel.telegram: FakeAdapter()})             # the question is in her chat now
    async with tx() as conn:                                                 # as Telegram numbers it
        await execute(conn, "update messages set external_id = '9001' where direction = 'out' and text = :t",
                      t=households.SHOPS_RENEW_OFFER)
    await post(client, {"update_id": 2, "message_reaction": {
        "chat": {"id": 1002, "type": "private"}, "message_id": 9001,
        "user": {"id": 1002, "is_bot": False, "first_name": "Ada"}, "date": 1791230640, "old_reaction": [],
        "new_reaction": [{"type": "emoji", "emoji": "\U0001F44D\U0001F3FD"}]}})
    assert await turn(home) == 0
    assert (await hashes())["Ada"] != before["Ada"]


async def test_a_link_made_for_an_old_address_is_replaced_at_once_and_one_of_unknown_address_is_asked_about(client):
    async with tx() as conn:
        home, _ = await household(conn)
    await made_for(OLD, "Ada")
    await made_for(None, "Ola")
    before = await hashes()

    await post(client, tg_update(1, " Shops. ", user_id=1002, name="Ada"))
    await post(client, tg_update(2, "shops", user_id=1001, name="Ola"))
    assert await turn(home) == 0
    (ada,) = await sent_to("Ada")
    token = URL.search(ada).group(1)
    assert ada == households.presence_text(f"http://testserver/presence/{token}", moved=True)
    assert ada.startswith("My web address has changed, so your old link had stopped working")
    assert await sent_to("Ola") == [households.SHOPS_RENEW_OFFER]           # it may still work: asked first
    after = await hashes()
    assert after["Ada"] == hash_token(token) and after["Ola"] == before["Ola"]


# ---------------------------------------------------------------- the address changes
async def a_household_after_a_move(conn):
    """Ola's and Ada's links were made at the old address; Grace's before addresses were kept;
    Bisi has none; Femi has one but never connected; Tobi is a child."""
    home, tokens = await household(conn)
    await add_member(conn, home, "Grace", telegram_id="1003")
    await add_member(conn, home, "Bisi", telegram_id="1004")
    await add_member(conn, home, "Femi")
    for who in ("Grace", "Femi", "Tobi"):
        tokens[who] = await members.new_presence_token(conn, home.members[who])
    await execute(conn, "update members set presence_link_base = :old where name in ('Ola', 'Ada', 'Femi', 'Tobi')",
                  old=OLD)
    await execute(conn, "update members set presence_link_base = null where name = 'Grace'")
    await execute(conn, "update members set quiet_start = '21:30', quiet_end = '07:00'")
    return home, tokens


async def notices():
    return sorted((r["name"], r["text"], r["respect_quiet_hours"]) for r in await rows(
        "select m.name, o.text, o.respect_quiet_hours from outbox o join members m on m.id = o.member_id "
        "where o.dedupe_key like 'presence_moved:%'"))


async def test_when_the_address_changes_each_adult_with_a_link_is_told_once_across_restarts_and_replicas():
    async with tx() as conn:
        await a_household_after_a_move(conn)

    assert await jobs.presence_links() == 3
    told = [("Ada", households.LINK_MOVED, True), ("Grace", households.LINK_MAYBE_MOVED, True),
            ("Ola", households.LINK_MOVED, True)]
    assert await notices() == told
    for text in (households.LINK_MOVED, households.LINK_MAYBE_MOVED):
        assert "send me the word shops" in text and technical_words(text) == []

    assert await jobs.presence_links() == 0                                  # the worker restarted
    assert await asyncio.gather(jobs.presence_links(), jobs.presence_links()) == [0, 0]   # two replicas
    assert await notices() == told


async def test_two_replicas_starting_together_after_a_move_send_each_notice_once():
    async with tx() as conn:
        await a_household_after_a_move(conn)
    assert sorted(await asyncio.gather(*(jobs.presence_links() for _ in range(4)))) == [0, 0, 0, 3]
    assert len(await notices()) == 3


async def test_the_notice_waits_for_quiet_hours_to_end():
    async with tx() as conn:
        home, _ = await a_household_after_a_move(conn)
    await jobs.presence_links()
    adapter = FakeAdapter()
    night = london("2026-10-27 02:00")
    await router.dispatch_due({Channel.telegram: adapter}, now=night)
    assert adapter.sent == []
    assert {r["send_after"] for r in await rows("select send_after from outbox")} == {london("2026-10-27 07:00")}
    await router.dispatch_due({Channel.telegram: adapter}, now=london("2026-10-27 07:00"))
    assert sorted(text for _, text, _ in adapter.sent) == sorted(
        [households.LINK_MOVED, households.LINK_MOVED, households.LINK_MAYBE_MOVED])


async def test_a_new_link_is_not_told_and_a_second_move_is_told_again(client, monkeypatch):
    async with tx() as conn:
        home, _ = await a_household_after_a_move(conn)
    await jobs.presence_links()
    await post(client, tg_update(1, "shops", user_id=1002, name="Ada"))     # Ada takes up the offer
    await turn(home)
    assert await jobs.presence_links() == 0

    monkeypatch.setattr(get_settings(), "public_base_url", "https://home.example.test/")
    assert await jobs.presence_links() == 3          # Ola and Grace again, and Ada's new link too
    assert sorted(name for name, _, _ in await notices()) == ["Ada", "Ada", "Grace", "Grace", "Ola", "Ola"]
    assert await jobs.presence_links() == 0


# ---------------------------------------------------------------- links made before addresses were kept
def _migration():
    path = Path(__file__).parents[2] / "migrations" / "versions" / "0005_presence_link_address.py"
    spec = importlib.util.spec_from_file_location("migration_0005", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_a_link_from_before_is_given_the_address_of_the_message_that_sent_it():
    """As on the server: two links sent in chat from a temporary address, then one made on Settings."""
    async with tx() as conn:
        home, tokens = await household(conn)
        grace = await add_member(conn, home, "Grace", telegram_id="1003")
        tokens["Grace"] = await members.new_presence_token(conn, grace)
        await execute(conn, "update members set presence_link_base = null")
        thread = await fetch_val(conn, "insert into threads (household_id, channel, external_thread_id, scope) "
                                       "values (:h, 'telegram', '1001', 'dm') returning id", h=home.id)
        stale = "Here is your personal link. Open it on your iPhone:\n{base}/presence/{token}\n\nKeep it to yourself."
        for at, base, who in ((london("2026-10-07 10:00"), OLD, "Ola"), (london("2026-10-09 10:00"), OLD, "Ada"),
                              (london("2026-10-08 10:00"), "http://somewhere.else", "Ada")):
            await execute(conn, "insert into messages (household_id, thread_id, direction, text, status, created_at) "
                                "values (:h, :t, 'out', :text, 'sent', :at)",
                          h=home.id, t=thread, text=stale.format(base=base, token=tokens[who]), at=at)
        await execute(conn, "insert into messages (household_id, thread_id, direction, text, status) "
                            "values (:h, :t, 'out', :text, 'sent')",      # someone else's, not a link of ours
                      h=home.id, t=thread, text=f"{OLD}/presence/{'x' * 43}")
        await execute(conn, _migration().BACKFILL)
    bases = {r["name"]: r["presence_link_base"] for r in await rows(
        "select name, presence_link_base from members where role = 'adult'")}
    assert bases == {"Ola": OLD, "Ada": OLD, "Grace": None}                   # the latest message that sent it
    assert await jobs.presence_links() == 3


# ---------------------------------------------------------------- the page and Settings
async def test_the_page_and_settings_say_when_a_link_was_made_for_an_old_address(client):
    from tests.unit.test_dashboard import login

    async with tx() as conn:
        home, tokens = await household(conn)
        await add_member(conn, home, "Grace", telegram_id="1003")
    await made_for(OLD, "Ada")
    ada = await client.get(f"/presence/{tokens['Ada']}")
    assert "This link was made for Home's old web address" in words_of(ada)
    assert f'data-copy="http://testserver/presence/{tokens["Ada"]}/enter/Tesco%20Extra"' in ada.text  # new address
    assert "old web address" not in words_of(await client.get(f"/presence/{tokens['Ola']}"))
    await made_for(None, "Ola")
    assert "old web address" not in words_of(await client.get(f"/presence/{tokens['Ola']}"))   # cannot tell

    await login(client, home)
    said = words_of(await client.get("/dashboard/settings"))
    assert "Ada has a link made for an old web address: it no longer works" in said
    assert "Ola has a link, perhaps made for an old web address" in said
    assert "Grace no link yet" in said


# ---------------------------------------------------------------- a shop's link asked for with GET
async def test_a_shop_link_asked_for_with_get_is_an_arrival_and_a_head_or_a_bad_link_records_nothing(client):
    async with tx() as conn:
        home, tokens = await household(conn)
        await on_list(conn, home, "bleach", shop="Tesco")
        await on_list(conn, home, "eggs")
    await client.head(f"/presence/{tokens['Ola']}/enter/Tesco%20Extra")       # a link preview's first look
    for path in ("/presence/never-issued/enter/Tesco%20Extra", f"/presence/{tokens['Ola']}/arrive/Tesco%20Extra",
                 f"/presence/{tokens['Ola']}"):
        await client.get(path)
    assert await pings() == []

    page = await client.get(f"/presence/{tokens['Ola']}/enter/Tesco%20Extra")
    assert page.status_code == 200 and "Home heard that you arrived at Tesco Extra" in words_of(page)
    assert page.headers["cache-control"] == "no-store"
    assert await pings() == [("Ola", "Tesco Extra", "enter")]
    assert await sent_to("Ola") == [TESCO_LIST.replace("\n\n- milk (probably)", "")]
    left = await client.get(f"/presence/{tokens['Ola']}/exit/Home")
    assert "Home heard that you left Home" in words_of(left)
    assert (await client.get("/presence/never-issued/enter/Tesco%20Extra")).status_code == 404
