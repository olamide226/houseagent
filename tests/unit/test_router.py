"""Outbound router: destinations, the allowlist property, quiet hours, degradation, splitting and retries."""
import random
from datetime import time, timedelta

import pytest

from app.channels.base import ChannelError, NotSupported, PermanentError
from app.core.envelope import Channel, DeliveryStatus, OutboundMessage
from app.core.timeutil import utcnow
from app.db import execute, fetch_all, fetch_one, fetch_val, tx
from app.pipeline import router
from app.pipeline.router import BACKOFF_SECONDS, enqueue, split_text
from tests.helpers import FakeAdapter, add_member, link, london, seed_home

OLA_WA = "GB.1000000000000000000101"
TEMPLATE = "household_reminder"


def whatsapp(**kwargs) -> FakeAdapter:
    """A stand-in with WhatsApp's rule: free-form text only within 24 hours of hearing from someone."""
    return FakeAdapter(Channel.whatsapp, window_hours=24, template=TEMPLATE, **kwargs)


async def thread(conn, home, external, scope="dm", channel="telegram"):
    return await fetch_val(
        conn, "insert into threads (household_id, channel, external_thread_id, scope) "
              "values (:h, :c, :e, :s) returning id", h=home.id, c=channel, e=external, s=scope)


async def outbox():
    async with tx() as conn:
        return await fetch_all(conn, "select * from outbox order by created_at")


async def random_outbox(rng: random.Random, rows: int = 300):
    """Two households on Telegram and WhatsApp with every kind of thread a row could point at,
    and `rows` random outbox rows. Returns who may be reached, per household and channel, and
    which household each message belongs to."""
    async with tx() as conn:
        homes = [await seed_home(conn, telegram_id="1001"), await seed_home(conn, telegram_id="2001")]
        allowed: dict[tuple[str, str], set[str]] = {}
        threads: list[str] = []
        members: list[str] = []
        for index, home in enumerate(homes):
            base = (index + 1) * 1000
            telegram = allowed[home.id, "telegram"] = {str(base + 1), str(base + 2)}
            whatsapp = allowed[home.id, "whatsapp"] = {f"GB.{base + 1}", f"GB.{base + 2}", f"+4477009{base + 3}"}
            await link(conn, home.ola, f"GB.{base + 1}", "whatsapp")
            ada = await add_member(conn, home, "Ada", telegram_id=str(base + 2), whatsapp_id=f"GB.{base + 2}")
            await execute(conn, "update members set preferred_channel = 'whatsapp' where id = :m", m=ada)
            await add_member(conn, home, "Bisi", whatsapp_id=f"+4477009{base + 3}")   # WhatsApp only, by number
            await add_member(conn, home, "Unlinked adult")                 # no channel identity
            await add_member(conn, home, "Tobi", role="child")
            members += list(home.members.values())
            threads.append(await thread(conn, home, str(base + 1)))          # a verified member's DM
            threads.append(await thread(conn, home, str(base + 666)))        # a DM with a stranger's handle
            threads.append(await thread(conn, home, f"GB.{base + 1}", channel="whatsapp"))
            threads.append(await thread(conn, home, f"GB.{base + 666}", channel="whatsapp"))      # a stranger
            threads.append(await thread(conn, home, f"+4477009{base + 666}", channel="whatsapp"))  # and another
            threads.append(await thread(conn, home, str(base + 1), channel="whatsapp"))  # a Telegram id is no WhatsApp handle
            threads.append(await thread(conn, home, f"ola{base}@example.com", channel="imessage"))   # no adapter
            for channel, group in (("telegram", f"-100{base}"), ("whatsapp", f"Z3JvdXA{base}ZD")):
                threads.append(await thread(conn, home, group, scope="group", channel=channel))
                (telegram if channel == "telegram" else whatsapp).add(group)
        # Only the first household has a primary thread, so the second fans out to adults.
        await execute(conn, "update households set primary_thread_id = :t where id = :h",
                      t=threads[8], h=homes[0].id)

        owner: dict[str, str] = {}
        for n in range(rows):
            home = rng.choice(homes)
            target = rng.choice(["thread", "member", "household"])
            marker = f"message-{n}"
            owner[marker] = home.id
            await execute(
                conn,
                """insert into outbox (household_id, target, thread_id, member_id, text, respect_quiet_hours)
                   values (:h, :target, :thread, :member, :text, false)""",
                h=home.id, target=target, text=marker,
                thread=rng.choice(threads) if target == "thread" or rng.random() < 0.2 else None,
                member=rng.choice(members) if target == "member" or rng.random() < 0.2 else None,
            )
    return allowed, owner


def reached(adapter: FakeAdapter) -> list[tuple[str, str]]:
    """Everything the adapter delivered, free-form or by template, as (chat, text)."""
    return [(chat, text) for chat, text, _ in adapter.sent] + [(chat, params[0]) for chat, _, params in adapter.templates]


async def test_nothing_is_ever_sent_to_a_handle_outside_channel_identities():
    """Property test: whatever rows land in outbox, sends on either channel only reach the
    household's own verified handles and its own group threads."""
    allowed, owner = await random_outbox(random.Random(1791237159))
    adapters = {Channel.telegram: FakeAdapter(), Channel.whatsapp: whatsapp()}
    while await router.dispatch_due(adapters):
        pass

    for channel, adapter in adapters.items():
        assert len(reached(adapter)) > 40, "the property must be exercised by real sends"
        for chat, text in reached(adapter):
            assert chat in allowed[owner[text], channel.value], f"{text} was sent to {chat} on {channel.value}"
    assert adapters[Channel.whatsapp].sent and adapters[Channel.whatsapp].templates    # both ways of sending
    statuses = {row["status"] for row in await outbox()}
    assert {"sent", "failed"} <= statuses and "pending" not in statuses


async def test_the_fallback_to_another_channel_also_stays_inside_the_family():
    """The same property while WhatsApp refuses everything: what moves to Telegram instead
    still only reaches that household's own verified Telegram handles."""
    allowed, owner = await random_outbox(random.Random(1791246922))
    telegram = FakeAdapter()
    adapters = {Channel.telegram: telegram,
                Channel.whatsapp: whatsapp(fail=PermanentError("whatsapp send failed: 131026"))}
    while await router.dispatch_due(adapters):
        pass

    for chat, text in reached(telegram):
        assert chat in allowed[owner[text], "telegram"], f"{text} was sent to {chat}"
    moved = [row for row in await outbox() if (row["dedupe_key"] or "").startswith("fallback:")]
    assert len([row for row in moved if row["status"] == "sent"]) > 10, "the fallback must be exercised"
    assert {row["channel_used"] for row in moved if row["status"] == "sent"} == {"telegram"}
    assert all(not chat.startswith("-100") for chat, text in reached(telegram)
               if text in {row["text"] for row in moved})                    # a fallback never lands in a group


async def test_member_target_goes_to_the_preferred_channel_dm():
    async with tx() as conn:
        home = await seed_home(conn)
        await enqueue(conn, OutboundMessage(household_id=home.id, target="member", member_id=home.ola, text="hello",
                                            respect_quiet_hours=False))
    adapter = FakeAdapter()
    assert await router.dispatch_due({Channel.telegram: adapter}) == 1
    assert adapter.sent == [("1001", "hello", None)]
    (row,) = await outbox()
    assert (row["status"], row["channel_used"], row["external_id"]) == ("sent", "telegram", "out-1")
    async with tx() as conn:
        out = await fetch_one(conn, "select direction, text, status, member_id from messages")
    assert out == {"direction": "out", "text": "hello", "status": "sent", "member_id": None}


async def test_household_target_uses_the_primary_thread_or_each_adult():
    async with tx() as conn:
        home = await seed_home(conn)
        await add_member(conn, home, "Ada", telegram_id="1002")
        await add_member(conn, home, "Tobi", role="child")
        await enqueue(conn, OutboundMessage(household_id=home.id, target="household", text="digest", dedupe_key="d1",
                                            respect_quiet_hours=False))
    adapter = FakeAdapter()
    while await router.dispatch_due({Channel.telegram: adapter}):
        pass
    assert sorted(chat for chat, _, _ in adapter.sent) == ["1001", "1002"]

    async with tx() as conn:
        group = await thread(conn, home, "-100555", scope="group")
        await execute(conn, "update households set primary_thread_id = :t where id = :h", t=group, h=home.id)
        await enqueue(conn, OutboundMessage(household_id=home.id, target="household", text="digest", dedupe_key="d2",
                                            respect_quiet_hours=False))
    adapter = FakeAdapter()
    while await router.dispatch_due({Channel.telegram: adapter}):
        pass
    assert adapter.sent == [("-100555", "digest", None)]


async def test_dedupe_key_makes_enqueue_idempotent():
    async with tx() as conn:
        home = await seed_home(conn)
        message = OutboundMessage(household_id=home.id, target="member", member_id=home.ola, text="x", dedupe_key="k")
        assert await enqueue(conn, message) is not None
        assert await enqueue(conn, message) is None
    assert len(await outbox()) == 1


async def ack(conn, home, external="501"):
    dm = await thread(conn, home, "1001")
    message = await fetch_val(
        conn, "insert into messages (household_id, thread_id, member_id, direction, text, external_id) "
              "values (:h, :t, :m, 'in', 'out of eggs', :e) returning id", h=home.id, t=dm, m=home.ola, e=external)
    await enqueue(conn, OutboundMessage(household_id=home.id, target="thread", thread_id=dm, react_emoji="ack",
                                        reply_to_message_id=message, respect_quiet_hours=False))


async def test_ack_becomes_the_adapters_reaction_on_the_message():
    async with tx() as conn:
        await ack(conn, await seed_home(conn))
    adapter = FakeAdapter()
    await router.dispatch_due({Channel.telegram: adapter})
    assert adapter.reactions == [("1001", "501", "\U0001F44D")] and adapter.sent == []
    async with tx() as conn:
        out = await fetch_one(conn, "select text, meta from messages where direction = 'out'")
    assert out == {"text": None, "meta": {"reaction": "\U0001F44D"}}


async def test_ack_degrades_to_a_text_emoji_when_reactions_are_not_supported():
    async with tx() as conn:
        await ack(conn, await seed_home(conn))
    adapter = FakeAdapter(reactions=False)
    await router.dispatch_due({Channel.telegram: adapter})
    assert adapter.sent == [("1001", "\U0001F44D", None)] and adapter.reactions == []


def test_long_text_splits_on_paragraphs_then_lines_then_hard():
    paragraphs = "\n\n".join(f"paragraph {n} " + "x" * 30 for n in range(6))
    chunks = split_text(paragraphs, 100)
    assert all(len(chunk) <= 100 for chunk in chunks) and len(chunks) > 1
    assert "\n\n".join(chunks) == paragraphs                       # split exactly on paragraph boundaries
    assert split_text("short", 100) == ["short"]
    assert split_text("line one\nline two", 12) == ["line one", "line two"]
    assert split_text("x" * 250, 100) == ["x" * 100, "x" * 100, "x" * 50]


async def test_long_replies_are_sent_in_parts_and_only_the_first_is_threaded():
    async with tx() as conn:
        home = await seed_home(conn)
        dm = await thread(conn, home, "1001")
        message = await fetch_val(
            conn, "insert into messages (household_id, thread_id, member_id, direction, text, external_id) "
                  "values (:h, :t, :m, 'in', 'list?', '77') returning id", h=home.id, t=dm, m=home.ola)
        text = "\n\n".join("item " * 500 for _ in range(3))
        await enqueue(conn, OutboundMessage(household_id=home.id, target="thread", thread_id=dm, text=text,
                                            reply_to_message_id=message, respect_quiet_hours=False))
    adapter = FakeAdapter()
    await router.dispatch_due({Channel.telegram: adapter})
    assert len(adapter.sent) == 3
    assert [reply for _, _, reply in adapter.sent] == ["77", None, None]


async def test_failed_sends_back_off_then_fail_and_can_be_retried():
    async with tx() as conn:
        home = await seed_home(conn)
        await enqueue(conn, OutboundMessage(household_id=home.id, target="member", member_id=home.ola, text="hi",
                                            respect_quiet_hours=False))
    broken = {Channel.telegram: FakeAdapter(fail=ChannelError("telegram is down"))}
    now = utcnow()
    for attempt, delay in enumerate(BACKOFF_SECONDS, start=1):
        assert await router.dispatch_due(broken, now=now) == 1
        (row,) = await outbox()
        assert (row["status"], row["attempts"], row["last_error"]) == ("pending", attempt, "telegram is down")
        assert row["send_after"] == now + timedelta(seconds=delay)
        assert await router.dispatch_due(broken, now=now) == 0     # not due until the backoff passes
        now = row["send_after"]
    await router.dispatch_due(broken, now=now)
    (row,) = await outbox()
    assert (row["status"], row["attempts"]) == ("failed", 6)

    async with tx() as conn:
        assert await router.retry(conn, home.id, row["id"])
    working = FakeAdapter()
    await router.dispatch_due({Channel.telegram: working})
    assert working.sent == [("1001", "hi", None)]
    assert (await outbox())[0]["status"] == "sent"


# ---------------------------------------------------------------- quiet hours
async def quiet(conn, member_id, start, end):
    await execute(conn, "update members set quiet_start = :s, quiet_end = :e where id = :id",
                  s=start and time.fromisoformat(start), e=end and time.fromisoformat(end), id=member_id)


async def send_at(now, adapter=None):
    adapter = adapter or FakeAdapter()
    while await router.dispatch_due({Channel.telegram: adapter}, now=now):
        pass
    return adapter


async def test_a_send_inside_quiet_hours_waits_for_the_morning_unless_it_is_urgent_or_a_reply():
    night, morning = london("2026-10-06 23:00"), london("2026-10-07 07:00")
    async with tx() as conn:
        home = await seed_home(conn)                                   # default quiet hours: 21:30 to 07:00
        dm = await thread(conn, home, "1001")
        for text, extra in [("held", {}), ("urgent", {"urgency": "high"}), ("a reply", {"respect_quiet_hours": False})]:
            await enqueue(conn, OutboundMessage(household_id=home.id, target="member", member_id=home.ola,
                                                text=text, **extra), send_after=night)
        await enqueue(conn, OutboundMessage(household_id=home.id, target="thread", thread_id=dm, text="held dm"),
                      send_after=night)

    adapter = await send_at(night)
    assert sorted(text for _, text, _ in adapter.sent) == ["a reply", "urgent"]
    held = [row for row in await outbox() if row["text"].startswith("held")]
    assert [(row["status"], row["send_after"], row["attempts"]) for row in held] == [("pending", morning, 0)] * 2

    await send_at(morning - timedelta(minutes=1), adapter)
    assert len(adapter.sent) == 2
    await send_at(morning, adapter)
    assert sorted(text for _, text, _ in adapter.sent[2:]) == ["held", "held dm"]


async def test_each_member_is_held_by_their_own_quiet_hours():
    evening = london("2026-10-06 21:00")
    async with tx() as conn:
        home = await seed_home(conn)
        ada = await add_member(conn, home, "Ada", telegram_id="1002")
        await add_member(conn, home, "Tobi", role="child")            # a child's hours never hold anything
        await quiet(conn, home.ola, None, None)                        # Ola: no quiet hours at all
        await quiet(conn, ada, "20:00", "06:00")
        await enqueue(conn, OutboundMessage(household_id=home.id, target="household", text="brief"),
                      send_after=evening)
    adapter = await send_at(evening)                                   # no group: one send per adult
    assert adapter.sent == [("1001", "brief", None)]
    await send_at(london("2026-10-07 06:00"), adapter)
    assert adapter.sent[1:] == [("1002", "brief", None)]


async def test_a_group_send_waits_until_no_adult_is_in_quiet_hours():
    async with tx() as conn:
        home = await seed_home(conn)                                   # Ola: 21:30 to 07:00
        ada = await add_member(conn, home, "Ada", telegram_id="1002")
        await quiet(conn, ada, "20:00", "06:00")
        group = await thread(conn, home, "-100555", scope="group")
        await execute(conn, "update households set primary_thread_id = :t where id = :h", t=group, h=home.id)
        await enqueue(conn, OutboundMessage(household_id=home.id, target="household", text="digest"),
                      send_after=london("2026-10-06 21:00"))

    adapter = await send_at(london("2026-10-06 21:00"))               # Ada is already in quiet hours
    (row,) = await outbox()
    assert adapter.sent == [] and row["send_after"] == london("2026-10-07 06:00")
    await send_at(london("2026-10-07 06:00"), adapter)                 # Ada is out, Ola is not
    (row,) = await outbox()
    assert adapter.sent == [] and row["send_after"] == london("2026-10-07 07:00")
    await send_at(london("2026-10-07 07:00"), adapter)
    assert adapter.sent == [("-100555", "digest", None)]


# ---------------------------------------------------------------- the WhatsApp 24-hour window
NOON = london("2026-10-06 12:00")


async def on_whatsapp(conn, *, connected=timedelta(days=30), heard=None, now=NOON):
    """Ola on WhatsApp only, connected `connected` ago, who last wrote `heard` ago (or never)."""
    home = await seed_home(conn, telegram_id=None)
    await link(conn, home.ola, OLA_WA, "whatsapp", verified_at=now - connected)
    dm = await thread(conn, home, OLA_WA, channel="whatsapp")
    if heard is not None:
        await hear(conn, home, dm, now - heard)
    return home, dm


async def hear(conn, home, thread_id, at, external="wamid.in"):
    return await fetch_val(
        conn, """insert into messages (household_id, thread_id, member_id, direction, text, external_id, status,
                                       created_at)
                 values (:h, :t, :m, 'in', 'we need milk', :e, 'processed', :at) returning id""",
        h=home.id, t=thread_id, m=home.ola, e=external, at=at)


async def tell(conn, home, text="Bins tonight", **message):
    await enqueue(conn, OutboundMessage(household_id=home.id, target="member", member_id=home.ola, text=text,
                                        respect_quiet_hours=False, **message), send_after=NOON)


@pytest.mark.parametrize("heard,templated", [
    (timedelta(minutes=1), False),
    (timedelta(hours=23, minutes=59, seconds=59), False),
    (timedelta(hours=24), True),                       # at the edge the window has closed
    (timedelta(hours=24, seconds=1), True),
    (timedelta(days=3), True),
    (None, True),                                      # never wrote, and connected a month ago
])
async def test_whatsapp_text_is_free_form_inside_24_hours_and_the_template_from_then_on(heard, templated):
    async with tx() as conn:
        home, _ = await on_whatsapp(conn, heard=heard)
        await tell(conn, home)
    adapter = whatsapp()
    assert await router.dispatch_due({Channel.whatsapp: adapter}, now=NOON) == 1

    if templated:
        assert adapter.templates == [(OLA_WA, TEMPLATE, ["Bins tonight"])] and adapter.sent == []
    else:
        assert adapter.sent == [(OLA_WA, "Bins tonight", None)] and adapter.templates == []
    (row,) = await outbox()
    assert (row["status"], row["channel_used"]) == ("sent", "whatsapp")
    async with tx() as conn:
        out = await fetch_one(conn, "select text, meta from messages where direction = 'out'")
    assert out == {"text": "Bins tonight", "meta": {"template": TEMPLATE} if templated else {}}


async def test_connecting_with_an_invite_code_opens_the_window_like_a_message():
    async with tx() as conn:
        home, _ = await on_whatsapp(conn, connected=timedelta(hours=1))   # the code is not kept as a message
        await tell(conn, home, "Hi Ola, you're connected.")
    adapter = whatsapp()
    await router.dispatch_due({Channel.whatsapp: adapter}, now=NOON)
    assert adapter.sent == [(OLA_WA, "Hi Ola, you're connected.", None)] and adapter.templates == []


async def test_a_group_has_its_own_window_and_telegram_has_none():
    async with tx() as conn:
        home, dm = await on_whatsapp(conn, heard=timedelta(hours=1))      # the DM is open...
        await link(conn, home.ola, "1001")
        group = await thread(conn, home, "Z3JvdXAZD", scope="group", channel="whatsapp")
        chat = await thread(conn, home, "-100555", scope="group")         # Telegram, never heard from
        for thread_id, text in [(group, "to the quiet group"), (chat, "to telegram")]:
            await enqueue(conn, OutboundMessage(household_id=home.id, target="thread", thread_id=thread_id, text=text,
                                                respect_quiet_hours=False), send_after=NOON)
    wa, telegram = whatsapp(), FakeAdapter()
    adapters = {Channel.whatsapp: wa, Channel.telegram: telegram}
    await router.dispatch_due(adapters, now=NOON)
    assert wa.templates == [("Z3JvdXAZD", TEMPLATE, ["to the quiet group"])]   # ...but nobody wrote in the group
    assert telegram.sent == [("-100555", "to telegram", None)] and telegram.templates == []

    async with tx() as conn:
        await hear(conn, home, group, NOON - timedelta(hours=2), external="wamid.group")
        await enqueue(conn, OutboundMessage(household_id=home.id, target="thread", thread_id=group, text="now open",
                                            respect_quiet_hours=False), send_after=NOON)
    await router.dispatch_due(adapters, now=NOON)
    assert wa.sent == [("Z3JvdXAZD", "now open", None)] and len(wa.templates) == 1


async def test_outside_the_window_long_text_is_cut_to_fit_and_an_ack_is_still_a_reaction():
    long = "\n".join(f"- item {n} " + "x" * 40 for n in range(40))
    async with tx() as conn:
        home, dm = await on_whatsapp(conn, heard=timedelta(days=2))
        await tell(conn, home, long)
        await enqueue(conn, OutboundMessage(
            household_id=home.id, target="thread", thread_id=dm, react_emoji="ack", respect_quiet_hours=False,
            reply_to_message_id=await fetch_val(conn, "select id from messages")), send_after=NOON)
    adapter = whatsapp()
    await router.dispatch_due({Channel.whatsapp: adapter}, now=NOON)
    ((chat, name, (param,)),) = adapter.templates
    assert (chat, name, len(param)) == (OLA_WA, TEMPLATE, 900) and param == long[:899] + "…"
    assert adapter.reactions == [(OLA_WA, "wamid.in", "\U0001F44D")] and adapter.sent == []
    async with tx() as conn:                                                # the record keeps what was meant
        assert await fetch_val(conn, "select text from messages where meta ? 'template'") == long


async def test_a_channel_with_a_window_but_no_template_support_sends_plain_text():
    class NoTemplates(FakeAdapter):
        async def send_template(self, external_thread_id, name, params):
            raise NotSupported

    async with tx() as conn:
        home, _ = await on_whatsapp(conn, heard=timedelta(days=2))
        await tell(conn, home)
    adapter = NoTemplates(Channel.whatsapp, window_hours=24, template=TEMPLATE)
    await router.dispatch_due({Channel.whatsapp: adapter}, now=NOON)
    assert adapter.sent == [(OLA_WA, "Bins tonight", None)]


# ---------------------------------------------------------------- next channel after a failure for good
async def on_both(conn, preferred="telegram"):
    """Ola on Telegram and WhatsApp, heard from on WhatsApp a moment ago."""
    home = await seed_home(conn)
    await link(conn, home.ola, OLA_WA, "whatsapp", verified_at=NOON - timedelta(minutes=1))
    await execute(conn, "update members set preferred_channel = :c where id = :m", c=preferred, m=home.ola)
    return home


async def drain(adapters, now=NOON):
    for _ in range(50):
        if not await router.dispatch_due(adapters, now=now):
            return
    raise AssertionError("the outbox never drains: sends keep making more sends")


async def test_changing_the_preferred_channel_row_is_all_it_takes_to_move_a_members_dms():
    adapters = {Channel.telegram: FakeAdapter(), Channel.whatsapp: whatsapp()}
    async with tx() as conn:
        home = await on_both(conn)
        await tell(conn, home, "one")
    await drain(adapters)
    async with tx() as conn:
        await execute(conn, "update members set preferred_channel = 'whatsapp' where id = :m", m=home.ola)
        await tell(conn, home, "two")
    await drain(adapters)
    async with tx() as conn:
        await execute(conn, "update members set preferred_channel = 'telegram' where id = :m", m=home.ola)
        await tell(conn, home, "three")
    await drain(adapters)
    assert adapters[Channel.telegram].sent == [("1001", "one", None), ("1001", "three", None)]
    assert adapters[Channel.whatsapp].sent == [(OLA_WA, "two", None)]
    assert [row["channel_used"] for row in await outbox()] == ["telegram", "whatsapp", "telegram"]


@pytest.mark.parametrize("preferred,other,first,second", [
    ("telegram", "whatsapp", "1001", OLA_WA), ("whatsapp", "telegram", OLA_WA, "1001")])
async def test_a_send_refused_for_good_goes_once_to_the_members_next_channel(preferred, other, first, second):
    async with tx() as conn:
        home = await on_both(conn, preferred)
        await tell(conn, home, urgency="high")
    adapters = {Channel(preferred): FakeAdapter(Channel(preferred), fail=PermanentError("blocked by the user")),
                Channel(other): FakeAdapter(Channel(other))}
    await drain(adapters)

    original, moved = await outbox()
    assert (original["status"], original["attempts"], original["channel_used"], original["last_error"]) == (
        "failed", 1, preferred, "blocked by the user")                       # no backoff: retrying cannot help
    assert (moved["status"], moved["channel_used"], moved["dedupe_key"]) == ("sent", other, f"fallback:{original['id']}")
    assert (moved["target"], moved["text"], moved["urgency"], moved["respect_quiet_hours"]) == (
        "thread", "Bins tonight", "high", False)
    assert adapters[Channel(other)].sent == [(second, "Bins tonight", None)]

    # Retried from the dashboard and refused again: the one fallback is not repeated.
    async with tx() as conn:
        assert await router.retry(conn, home.id, original["id"])
    assert (await outbox())[0]["status"] == "pending"
    await drain(adapters, now=london("2100-01-01 00:00"))                    # whenever the retry was clicked
    assert [row["status"] for row in await outbox()] == ["failed", "sent"]
    assert len(adapters[Channel(other)].sent) == 1


async def test_a_fallback_that_fails_too_is_not_passed_on_again():
    async with tx() as conn:
        await tell(conn, await on_both(conn))
    adapters = {Channel.telegram: FakeAdapter(fail=PermanentError("blocked")),
                Channel.whatsapp: whatsapp(fail=PermanentError("131026 undeliverable"))}
    await drain(adapters)
    rows = await outbox()
    assert [(row["status"], row["channel_used"]) for row in rows] == [("failed", "telegram"), ("failed", "whatsapp")]


async def test_a_send_that_keeps_failing_moves_on_only_after_its_last_retry():
    async with tx() as conn:
        await tell(conn, await on_both(conn))
    wa = whatsapp()
    adapters = {Channel.telegram: FakeAdapter(fail=ChannelError("telegram is down")), Channel.whatsapp: wa}
    now = NOON
    for _ in BACKOFF_SECONDS:
        await router.dispatch_due(adapters, now=now)
        (row,) = await outbox()
        assert row["status"] == "pending" and wa.sent == []
        now = row["send_after"]
    await drain(adapters, now=now)
    assert [row["status"] for row in await outbox()] == ["failed", "sent"]
    assert reached(wa) == [(OLA_WA, "Bins tonight")]


async def test_only_a_text_to_one_person_with_another_channel_falls_back():
    refused = PermanentError("refused")
    async with tx() as conn:
        home = await on_both(conn)
        ada = await add_member(conn, home, "Ada", telegram_id="1002")        # one channel only
        group = await thread(conn, home, "-100555", scope="group")
        dm = await thread(conn, home, "1001")
        heard = await hear(conn, home, dm, NOON, external="501")
        for message in [
            OutboundMessage(household_id=home.id, target="member", member_id=ada, text="to Ada"),
            OutboundMessage(household_id=home.id, target="thread", thread_id=group, text="to the group"),
            OutboundMessage(household_id=home.id, target="thread", thread_id=dm, react_emoji="ack",
                            reply_to_message_id=heard),
        ]:
            message.respect_quiet_hours = False
            await enqueue(conn, message, send_after=NOON)
    wa = whatsapp()
    await drain({Channel.telegram: FakeAdapter(fail=refused), Channel.whatsapp: wa}, now=NOON)
    assert [row["status"] for row in await outbox()] == ["failed"] * 3
    assert reached(wa) == [] and wa.reactions == []


async def test_a_reply_in_a_dm_that_cannot_be_delivered_follows_the_member_to_the_next_channel():
    async with tx() as conn:
        home = await on_both(conn)
        dm = await thread(conn, home, OLA_WA, channel="whatsapp")
        await enqueue(conn, OutboundMessage(household_id=home.id, target="thread", thread_id=dm, text="Done.",
                                            respect_quiet_hours=False), send_after=NOON)
    telegram = FakeAdapter()
    await drain({Channel.telegram: telegram, Channel.whatsapp: whatsapp(fail=PermanentError("190 token expired"))},
                now=NOON)
    assert telegram.sent == [("1001", "Done.", None)]


async def test_a_delivery_failure_reported_later_fails_the_send_and_moves_it_once():
    adapters = {Channel.telegram: FakeAdapter(), Channel.whatsapp: whatsapp()}
    async with tx() as conn:
        home = await on_both(conn, "whatsapp")
        await tell(conn, home)
    await drain(adapters, now=NOON)
    (sent,) = await outbox()
    assert (sent["status"], sent["channel_used"], sent["external_id"]) == ("sent", "whatsapp", "out-1")

    def failure(channel=Channel.whatsapp, external="out-1"):
        return DeliveryStatus(channel=channel, external_message_id=external, status="failed",
                              error="131047 Re-engagement message")

    async with tx() as conn:
        assert not await router.delivery_failed(conn, adapters, failure(external="someone-elses"))
        assert not await router.delivery_failed(conn, adapters, failure(channel=Channel.telegram))
        assert await router.delivery_failed(conn, adapters, failure(), now=NOON)
        assert not await router.delivery_failed(conn, adapters, failure(), now=NOON)      # Meta repeats itself
    await drain(adapters, now=NOON)
    original, moved = await outbox()
    assert (original["status"], original["last_error"]) == ("failed", "131047 Re-engagement message")
    assert (moved["status"], moved["channel_used"]) == ("sent", "telegram")
    assert adapters[Channel.telegram].sent == [("1001", "Bins tonight", None)]
