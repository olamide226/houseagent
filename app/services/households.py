"""Household bootstrap (spec section 12.1), onboarding state, the brief time, facts, places,
presence links, the calendar feed token, and the household's chats and primary group."""
import re
import secrets
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy.ext.asyncio import AsyncConnection

from app.agent.actions import Recorder
from app.agent.base import ToolError
from app.config import get_settings
from app.core.envelope import GroupUpdate, OutboundMessage
from app.core.identity import hash_token
from app.db import execute, fetch_all, fetch_one, fetch_val, jsonb
from app.pipeline.router import enqueue
from app.services import inventory, members

log = structlog.get_logger()
# Spec section 12.2, in order. The last one asks nothing: code sends each adult their link (ADR 0025).
PRESENCE = "presence"
ONBOARDING_STEPS = ("family", "routines", "shops", "staples", "tour", "rhythm", PRESENCE)
PLACE_KINDS = ("home", "store", "school", "clinic", "other")
SETTING_KEYS = ("staples", "morning_brief", "quiet_hours")   # said like facts, stored as settings (ADR 0016)
STORE_KEYS = ("shops", "main_supermarket")                   # facts whose values are also `places`
PENDING = "pending:"   # a group asked for but not yet created: its thread is `pending:{subject}` until then
SUBJECT_MAX = 128      # WhatsApp's limit on a group's name


def fact_key(raw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")


def names(value: str) -> list[str]:
    """A comma-separated list as people type it."""
    return [name for name in re.split(r"\s*[,;\n]\s*", value.strip()) if name]


async def household_exists(conn: AsyncConnection) -> bool:
    return bool(await fetch_val(conn, "select exists (select 1 from households)"))


async def create_household(conn: AsyncConnection, name: str, timezone: str, admin_name: str) -> tuple[str, str]:
    """Create the household, its admin member and the seed locations. Returns (household, admin) ids."""
    ZoneInfo(timezone)  # raises on an unknown zone before anything is written
    household_id = str(await fetch_val(
        conn, "insert into households (name, timezone) values (:name, :tz) returning id", name=name, tz=timezone
    ))
    member_id = str(await fetch_val(
        conn,
        "insert into members (household_id, name, role, is_admin) values (:h, :name, 'adult', true) returning id",
        h=household_id, name=admin_name,
    ))
    for location, aliases in inventory.DEFAULT_LOCATIONS.items():
        await inventory.create_location(conn, household_id, location, aliases)
    return household_id, member_id


async def timezone(conn: AsyncConnection, household_id: str) -> str:
    return str(await fetch_val(conn, "select timezone from households where id = :h", h=household_id))


async def new_calendar_token(conn: AsyncConnection, household_id: str) -> str:
    """A fresh ICS feed token, replacing any earlier one. Only its hash is stored, so it is shown once."""
    token = secrets.token_urlsafe(32)
    await execute(conn, "update households set calendar_token_hash = :hash where id = :h",
                  hash=hash_token(token), h=household_id)
    return token


async def for_calendar_token(conn: AsyncConnection, token: str) -> dict[str, Any] | None:
    return await fetch_one(conn, "select id, name, timezone from households where calendar_token_hash = :hash",
                           hash=hash_token(token))


# ---------------------------------------------------------------- onboarding
async def onboarding(conn: AsyncConnection, household_id: str) -> dict[str, Any]:
    """`{"step": current or None, "remaining": [...]}`; step is None once setup is complete."""
    state = await fetch_val(conn, "select onboarding_state from households where id = :h", h=household_id)
    remaining = [step for step in ONBOARDING_STEPS if step not in state.get("done", [])]
    if state.get("step") is None:
        remaining = []
    return {"step": remaining[0] if remaining else None, "remaining": remaining}


async def advance_onboarding(rec: Recorder, step: str, skipped: bool = False) -> None:
    """Mark a step done or skipped and move to the first step still open."""
    if step not in ONBOARDING_STEPS:
        raise ToolError(f"no step called {step}; the steps are {', '.join(ONBOARDING_STEPS)}")
    conn, household_id = rec.ctx.conn, rec.ctx.household_id
    state = await fetch_val(conn, "select onboarding_state from households where id = :h for update",
                            h=household_id)
    if state.get("step") is None:
        raise ToolError("setup is already complete")
    done = [*state.get("done", []), *([] if step in state.get("done", []) else [step])]
    was_skipped = [*state.get("skipped", []), *([step] if skipped else [])]
    notes = []
    if (step == PRESENCE and not skipped) or [name for name in ONBOARDING_STEPS if name not in done] == [PRESENCE]:
        # Nothing to ask for presence: when it is asked for, or is all that is left, the offer goes out.
        done += [] if PRESENCE in done else [PRESENCE]
        sent = await offer_shops(conn, household_id, speaker=rec.ctx.member_id)
        if sent:
            notes.append(f"NOTE: {', '.join(sent)} got a private message offering the shopping list on arrival at "
                         "a shop; it is optional, and sending the word shops starts it")
    remaining = [name for name in ONBOARDING_STEPS if name not in done]
    await rec.before("households", id=household_id)
    await execute(
        conn, "update households set onboarding_state = cast(:state as jsonb) where id = :h", h=household_id,
        state=jsonb({"step": remaining[0] if remaining else None, "done": done, "skipped": was_skipped}),
    )
    rec.lines.append(f"OK: {step} {'skipped' if skipped else 'done'}. "
                     + (f"Next step: {remaining[0]}" if remaining else "Setup is complete"))
    rec.lines += notes


# ---------------------------------------------------------------- presence links
def presence_url(token: str) -> str:
    return f"{get_settings().public_base_url}/presence/{token}"


# What setup sends each adult. It holds no link: the steps are on the page the link opens, and
# the link is made when someone asks for it (ADR 0033).
SHOPS_OFFER = ("One more thing, and it is optional: your iPhone can tell me when you arrive at a shop, so the list "
               "for that shop is waiting for you. It takes a few minutes to set up. Whenever you want to try it, "
               "send me the word shops.")
SHOPS_ALREADY = ("You already have your personal link: it is in the message I sent you before. Lost it? Send me "
                 "the word dashboard, open Settings, and tap Replace link.")


def presence_text(url: str) -> str:
    """What an adult is sent with their personal link. The page it opens says what to do."""
    return ("Here is your personal link. Open it on your iPhone and it shows you what to do, one shop at a time:\n"
            f"{url}\n\n"
            "Keep it to yourself: it is how I know it is your phone.")


async def presence_offered(conn: AsyncConnection, household_id: str) -> bool:
    """Whether setup made the offer (and did not skip it): later adults then get it too."""
    state = await fetch_val(conn, "select onboarding_state from households where id = :h", h=household_id)
    return PRESENCE in state.get("done", []) and PRESENCE not in state.get("skipped", [])


async def offer_shops(conn: AsyncConnection, household_id: str, *, member_id: str | None = None,
                      speaker: str | None = None) -> list[str]:
    """DM each connected adult who has no personal link the offer, once ever; `member_id` limits
    it to one person. Returns the names of those it went to."""
    adults = await fetch_all(
        conn,
        """select m.id, m.name from members m
           where m.household_id = :h and m.role = 'adult' and m.presence_token_hash is null
             and (cast(:member as uuid) is null or m.id = :member)
             and exists (select 1 from channel_identities ci where ci.member_id = m.id)
           order by m.created_at, m.name""",
        h=household_id, member=member_id,
    )
    return [adult["name"] for adult in adults if await enqueue(conn, OutboundMessage(
        household_id=household_id, target="member", member_id=adult["id"], text=SHOPS_OFFER,
        respect_quiet_hours=adult["id"] != speaker,   # whoever is talking to us now is awake
        dedupe_key=f"shops_offer:{adult['id']}",
    ))]


async def shops_link(conn: AsyncConnection, member_id: str) -> str:
    """The answer to the word "shops": a first personal link. One someone already has is left
    alone, because a new one would stop the shortcuts on their phone working."""
    if await fetch_val(conn, "select presence_token_hash is not null from members where id = :m", m=member_id):
        return SHOPS_ALREADY
    return presence_text(presence_url(await members.new_presence_token(conn, member_id)))


async def claim_nudge(conn: AsyncConnection, household_id: str, key: str, now: datetime, *,
                      again_after: timedelta | None = None) -> bool:
    """Whether a proactive nudge may go out under `key`: the first time, and again once
    `again_after` has passed since the last. One atomic statement, so of two callers at the
    same moment exactly one is told yes."""
    return bool(await fetch_val(
        conn,
        """insert into nudge_log (household_id, dedupe_key, sent_at) values (:h, :key, :now)
           on conflict (household_id, dedupe_key) do update set sent_at = excluded.sent_at
             where nudge_log.sent_at <= cast(:before as timestamptz)
           returning 1""",
        h=household_id, key=key, now=now, before=now - again_after if again_after else None,
    ))


IMESSAGE_OUTAGE = "imessage_outage"   # nudge_log key a household holds for as long as BlueBubbles is down


async def end_nudge(conn: AsyncConnection, key: str) -> bool:
    """Forget a claim in every household, so the next `claim_nudge` under `key` is a first time."""
    return bool(await execute(conn, "delete from nudge_log where dedupe_key = :key", key=key))


async def nudged_at(conn: AsyncConnection, household_id: str, key: str) -> datetime | None:
    at: datetime | None = await fetch_val(
        conn, "select sent_at from nudge_log where household_id = :h and dedupe_key = :key", h=household_id, key=key)
    return at


# ---------------------------------------------------------------- settings and facts
async def digest_time(conn: AsyncConnection, household_id: str) -> time:
    found: time = await fetch_val(conn, "select digest_time from households where id = :h", h=household_id)
    return found


async def set_digest_time(rec: Recorder, at: time) -> None:
    await rec.before("households", id=rec.ctx.household_id)
    await execute(rec.ctx.conn, "update households set digest_time = :at where id = :h",
                  at=at, h=rec.ctx.household_id)
    rec.lines.append(f"OK: morning brief at {at:%H:%M}")


async def facts(conn: AsyncConnection, household_id: str) -> list[dict[str, Any]]:
    return await fetch_all(
        conn,
        """select f.id, f.key, f.value, f.member_id, m.name as member from household_facts f
           left join members m on m.id = f.member_id where f.household_id = :h order by m.name nulls first, f.key""",
        h=household_id,
    )


async def set_fact(rec: Recorder, key: str, value: str | None, member_id: str | None = None) -> None:
    """Upsert a fact about the household, or about one member; no value forgets it."""
    conn, household_id = rec.ctx.conn, rec.ctx.household_id
    key, value = fact_key(key), (value or "").strip()
    if not key:
        raise ToolError("a fact needs a name")
    if key in SETTING_KEYS:
        raise ToolError(f"{key} is a setting, not a fact")
    if member_id is not None and not await fetch_val(
            conn, "select exists (select 1 from members where id = :m and household_id = :h)",
            m=member_id, h=household_id):
        raise ToolError("nobody like that is in the family")
    fact_id = await fetch_val(
        conn, "select id from household_facts where household_id = :h and key = :key "
              "and member_id is not distinct from :member", h=household_id, key=key, member=member_id)
    if fact_id is None and not value:
        rec.lines.append(f"OK: nothing was remembered as {key}")
        return
    if fact_id is None:
        fact_id = await fetch_val(
            conn, "insert into household_facts (household_id, member_id, key, value, updated_at) "
                  "values (:h, :member, :key, :value, clock_timestamp()) returning id",
            h=household_id, member=member_id, key=key, value=value)
        rec.created("household_facts", str(fact_id))
    else:
        await rec.before("household_facts", id=str(fact_id))
        if value:
            await execute(conn, "update household_facts set value = :value, updated_at = clock_timestamp() "
                                "where id = :id", value=value, id=fact_id)
        else:
            await execute(conn, "delete from household_facts where id = :id", id=fact_id)
    rec.lines.append(f"OK: {key} = {value}" if value else f"OK: forgot {key}")
    if key in STORE_KEYS:
        await add_stores(rec, names(value))


async def add_stores(rec: Recorder, names: list[str]) -> None:
    """Shops the family uses, as `places` of kind store: what a store-arrival Shortcut names."""
    for name in names:
        if await set_place(rec, name, "store"):
            rec.lines.append(f"NEW: {name} (shop)")


# ---------------------------------------------------------------- places
async def places(conn: AsyncConnection, household_id: str) -> list[dict[str, Any]]:
    return await fetch_all(conn, "select id, name, kind from places where household_id = :h order by kind, name",
                           h=household_id)


async def set_place(rec: Recorder, name: str, kind: str) -> bool:
    """Add a place, or change what kind of place it is. True if it was new."""
    name = " ".join(name.split())
    if not name or kind not in PLACE_KINDS:
        raise ToolError(f"a place needs a name and a kind: {', '.join(PLACE_KINDS)}")
    place = await fetch_one(
        rec.ctx.conn, "select id, kind from places where household_id = :h and lower(name) = lower(:name)",
        h=rec.ctx.household_id, name=name)
    if place is None:
        place_id = await fetch_val(
            rec.ctx.conn, "insert into places (household_id, name, kind) values (:h, :name, :kind) returning id",
            h=rec.ctx.household_id, name=name, kind=kind)
        rec.created("places", str(place_id))
    elif place["kind"] != kind:
        await rec.before("places", id=place["id"])
        await execute(rec.ctx.conn, "update places set kind = :kind where id = :id", kind=kind, id=place["id"])
    return place is None


async def place_named(conn: AsyncConnection, household_id: str, name: str) -> dict[str, Any]:
    """The place a Shortcut names. An unknown name becomes a new place: `home` if it is called
    Home, otherwise `other`, to be given its kind on the Settings page."""
    found = "select id, name, kind from places where household_id = :h and lower(name) = lower(:name) limit 1"
    place = await fetch_one(conn, found, h=household_id, name=name)
    if place is None:
        await execute(
            conn, "insert into places (household_id, name, kind) values (:h, :name, :kind) on conflict do nothing",
            h=household_id, name=name, kind="home" if name.lower() == "home" else "other")
        place = await fetch_one(conn, found, h=household_id, name=name)
    assert place is not None
    return place


# ---------------------------------------------------------------- chats and the primary group
async def threads(conn: AsyncConnection, household_id: str) -> list[dict[str, Any]]:
    """The household's chats on real channels, groups first, with when each was last heard from."""
    return await fetch_all(
        conn,
        """select t.id, t.channel, t.scope, t.external_thread_id, t.id = h.primary_thread_id as is_primary,
                  (select max(m.created_at) from messages m
                   where m.thread_id = t.id and m.direction = 'in') as last_in
           from threads t join households h on h.id = t.household_id
           where t.household_id = :h and t.channel in ('telegram', 'whatsapp', 'imessage')
           order by t.scope desc, t.created_at""",
        h=household_id,
    )


async def group_thread(conn: AsyncConnection, household_id: str, thread_id: str) -> dict[str, Any]:
    """One of the household's group chats that exists on its channel, or a ToolError."""
    thread = await fetch_one(
        conn, "select id, channel, external_thread_id from threads "
              "where id = :id and household_id = :h and scope = 'group'", id=thread_id, h=household_id)
    if thread is None or thread["external_thread_id"].startswith(PENDING):
        raise ToolError("that is not one of this household's group chats")
    return thread


async def set_primary_thread(rec: Recorder, thread_id: str) -> None:
    """Which group gets what is addressed to the whole household (briefs, shared reminders)."""
    thread = await group_thread(rec.ctx.conn, rec.ctx.household_id, thread_id)
    await execute(rec.ctx.conn, "update households set primary_thread_id = :t where id = :h",
                  t=thread_id, h=rec.ctx.household_id)
    rec.appended("threads", thread_id)
    rec.lines.append(f"OK: the {thread['channel']} group is now the family's main chat")


async def start_group(rec: Recorder, channel: str, subject: str) -> str:
    """Note a group that is about to be asked for. Returns the subject as it will be sent."""
    subject = " ".join(subject.split())
    if not subject or len(subject) > SUBJECT_MAX:
        raise ToolError(f"give the group a name of up to {SUBJECT_MAX} characters")
    thread_id = await fetch_val(
        rec.ctx.conn,
        """insert into threads (household_id, channel, external_thread_id, scope)
           values (:h, :channel, :pending, 'group') on conflict do nothing returning id""",
        h=rec.ctx.household_id, channel=channel, pending=PENDING + subject,
    )
    if thread_id is None:
        raise ToolError(f"a group called {subject} is already being created")
    rec.appended("threads", str(thread_id))
    rec.lines.append(f"OK: asked {channel} for a group called {subject}")
    return subject


async def forget_group(rec: Recorder, thread_id: str) -> None:
    """Drop a group that was asked for and never confirmed, so it can be asked for again."""
    deleted = await fetch_val(
        rec.ctx.conn, "delete from threads where id = :id and household_id = :h and scope = 'group' "
                      "and external_thread_id like :pending returning external_thread_id",
        id=thread_id, h=rec.ctx.household_id, pending=PENDING + "%")
    if deleted is None:
        raise ToolError("that is not a group waiting to be created")
    rec.appended("threads", thread_id)
    rec.lines.append(f"OK: no longer waiting for the group {deleted.removeprefix(PENDING)}")


async def finish_group(conn: AsyncConnection, update: GroupUpdate) -> None:
    """The channel's answer about a group we asked for: it becomes the household's primary
    thread, or on failure the request is dropped. A repeat, or a group nobody asked for, does nothing."""
    pending = await fetch_one(
        conn, "select id, household_id from threads where channel = :channel and external_thread_id = :pending "
              "for update", channel=update.channel.value, pending=PENDING + update.subject)
    if pending is None:
        return
    if update.external_thread_id is None:
        await execute(conn, "delete from threads where id = :id", id=pending["id"])
        log.warning("group_not_created", household_id=pending["household_id"], channel=update.channel.value,
                    error=update.error)
        return
    known = await fetch_one(
        conn, "select id, household_id from threads where channel = :channel and external_thread_id = :external",
        channel=update.channel.value, external=update.external_thread_id)
    if known is None:
        await execute(conn, "update threads set external_thread_id = :external where id = :id",
                      external=update.external_thread_id, id=pending["id"])
    else:   # someone wrote in the new group before this arrived, so its thread is already here
        await execute(conn, "delete from threads where id = :id", id=pending["id"])
        if known["household_id"] != pending["household_id"]:
            return
    await execute(conn, "update households set primary_thread_id = :t where id = :h",
                  t=(known or pending)["id"], h=pending["household_id"])
    log.info("group_created", household_id=pending["household_id"], channel=update.channel.value)


# ---------------------------------------------------------------- export and the worker's heartbeat
_MEMBERS = "(select id from members where household_id = :h)"
_ITEMS = "(select id from items where household_id = :h)"
# Every table that holds a household's data, and how its rows belong to one. `login_tokens` is
# left out: one-time links that are spent or expire within ten minutes.
EXPORTED = {
    "households": "id = :h", "members": "household_id = :h", "channel_identities": f"member_id in {_MEMBERS}",
    "threads": "household_id = :h", "messages": "household_id = :h", "household_facts": "household_id = :h",
    "locations": "household_id = :h", "items": "household_id = :h", "inventory_events": "household_id = :h",
    "stock": f"item_id in {_ITEMS}", "consumption_profiles": f"item_id in {_ITEMS}",
    "shopping_list_items": "household_id = :h", "events": "household_id = :h", "reminders": "household_id = :h",
    "places": "household_id = :h", "presence_events": f"member_id in {_MEMBERS}", "nudge_log": "household_id = :h",
    "agent_actions": "household_id = :h", "outbox": "household_id = :h", "job_runs": "household_id = :h",
}
HEARTBEAT = "worker_heartbeat"   # a job_runs row per household that the worker keeps moving forward


async def export(conn: AsyncConnection, household_id: str) -> dict[str, list[dict[str, Any]]]:
    """Everything the household has stored, table by table. Token hashes are left out: they
    are no use to their owner and only a risk in a file."""
    tables: dict[str, list[dict[str, Any]]] = {}
    for table, mine in EXPORTED.items():
        rows = await fetch_all(conn, f"select to_jsonb(t) as row from {table} t where {mine} order by 1",
                               h=household_id)
        tables[table] = [{k: v for k, v in r["row"].items() if not k.endswith("_hash")} for r in rows]
    return tables


async def beat(conn: AsyncConnection, now: datetime) -> None:
    """The worker says it is alive, in every household's `job_runs`, where the System page reads it."""
    await execute(
        conn, "insert into job_runs (job, household_id, run_key, ran_at) "
              "select :job, id, 'latest', :now from households "
              "on conflict (job, household_id, run_key) do update set ran_at = excluded.ran_at", job=HEARTBEAT, now=now)


async def job_runs(conn: AsyncConnection, household_id: str,
                   limit: int = 30) -> tuple[datetime | None, list[dict[str, Any]]]:
    """When the worker last said it was alive, and the newest runs of the once-a-day jobs."""
    seen: datetime | None = await fetch_val(
        conn, "select ran_at from job_runs where household_id = :h and job = :job", h=household_id, job=HEARTBEAT)
    runs = await fetch_all(
        conn, "select job, run_key, ran_at from job_runs where household_id = :h and job <> :job "
              "order by ran_at desc, job limit :limit", h=household_id, job=HEARTBEAT, limit=limit)
    return seen, runs
