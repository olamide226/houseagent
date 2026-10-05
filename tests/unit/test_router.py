"""Outbound router: destinations, the allowlist property, degradation, splitting and retries."""
import random
from datetime import timedelta

from app.channels.base import ChannelError
from app.core.envelope import Channel, OutboundMessage
from app.core.timeutil import utcnow
from app.db import execute, fetch_all, fetch_one, fetch_val, tx
from app.pipeline import router
from app.pipeline.router import BACKOFF_SECONDS, enqueue, split_text
from tests.helpers import FakeAdapter, add_member, seed_home


async def thread(conn, home, external, scope="dm", channel="telegram"):
    return await fetch_val(
        conn, "insert into threads (household_id, channel, external_thread_id, scope) "
              "values (:h, :c, :e, :s) returning id", h=home.id, c=channel, e=external, s=scope)


async def outbox():
    async with tx() as conn:
        return await fetch_all(conn, "select * from outbox order by created_at")


async def test_nothing_is_ever_sent_to_a_handle_outside_channel_identities():
    """Property test: whatever rows land in outbox, sends only reach the household's own
    verified handles and its own group threads."""
    rng = random.Random(1791237159)
    async with tx() as conn:
        homes = [await seed_home(conn, telegram_id="1001"), await seed_home(conn, telegram_id="2001")]
        allowed: dict[str, set[str]] = {homes[0].id: {"1001"}, homes[1].id: {"2001"}}
        threads: list[str] = []
        members: list[str] = []
        for index, home in enumerate(homes):
            base = (index + 1) * 1000
            await add_member(conn, home, "Ada", telegram_id=str(base + 2))
            allowed[home.id].add(str(base + 2))
            await add_member(conn, home, "Unlinked adult")                 # no channel identity
            await add_member(conn, home, "Tobi", role="child")
            members += list(home.members.values())
            threads.append(await thread(conn, home, str(base + 1)))          # a verified member's DM
            threads.append(await thread(conn, home, str(base + 666)))        # a DM with a stranger's handle
            threads.append(await thread(conn, home, f"+4477009{base}", channel="whatsapp"))   # no adapter
            group = f"-100{base}"
            threads.append(await thread(conn, home, group, scope="group"))
            allowed[home.id].add(group)
        # Only the first household has a primary thread, so the second fans out to adults.
        await execute(conn, "update households set primary_thread_id = :t where id = :h",
                      t=threads[3], h=homes[0].id)

        owner: dict[str, str] = {}
        for n in range(300):
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

    adapter = FakeAdapter()
    while await router.dispatch_due({Channel.telegram: adapter}):
        pass

    assert len(adapter.sent) > 50, "the property must be exercised by real sends"
    for chat, text, _ in adapter.sent:
        assert chat in allowed[owner[text]], f"{text} was sent to {chat}"
    statuses = {row["status"] for row in await outbox()}
    assert {"sent", "failed"} <= statuses and "pending" not in statuses


async def test_member_target_goes_to_the_preferred_channel_dm():
    async with tx() as conn:
        home = await seed_home(conn)
        await enqueue(conn, OutboundMessage(household_id=home.id, target="member", member_id=home.ola, text="hello"))
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
        await enqueue(conn, OutboundMessage(household_id=home.id, target="household", text="digest", dedupe_key="d1"))
    adapter = FakeAdapter()
    while await router.dispatch_due({Channel.telegram: adapter}):
        pass
    assert sorted(chat for chat, _, _ in adapter.sent) == ["1001", "1002"]

    async with tx() as conn:
        group = await thread(conn, home, "-100555", scope="group")
        await execute(conn, "update households set primary_thread_id = :t where id = :h", t=group, h=home.id)
        await enqueue(conn, OutboundMessage(household_id=home.id, target="household", text="digest", dedupe_key="d2"))
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
                                        reply_to_message_id=message))


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
                                            reply_to_message_id=message))
    adapter = FakeAdapter()
    await router.dispatch_due({Channel.telegram: adapter})
    assert len(adapter.sent) == 3
    assert [reply for _, _, reply in adapter.sent] == ["77", None, None]


async def test_failed_sends_back_off_then_fail_and_can_be_retried():
    async with tx() as conn:
        home = await seed_home(conn)
        await enqueue(conn, OutboundMessage(household_id=home.id, target="member", member_id=home.ola, text="hi"))
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
