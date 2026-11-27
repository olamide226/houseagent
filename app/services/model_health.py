"""Whether the model is answering, and telling the admins when it is not (ADR 0043).

The model is the installation's, so an outage is too: one `model_outages` row is open while turns
fail, whichever household's turn it was. A failure the admin has to fix (a refused or missing key,
an account out of credit, a model the service does not have, a subscription CLI signed out) tells
every household's admins at once; one that passes by itself (a timeout, an overloaded or rate-
limited service) only once every turn has failed for `TRANSIENT_ALERT_AFTER`. While it lasts the
admins are reminded at most once a day, on a turn that failed again; the first turn that works
closes it and tells whoever was told that the assistant is answering again.

Nothing here calls the model: the messages are fixed words that go out through the outbox like a
reminder. Every step is one atomic statement on the open row, so two replicas, or a restart,
never tell anyone twice."""
from datetime import datetime, timedelta
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.envelope import OutboundMessage
from app.core.timeutil import local
from app.db import execute, fetch_all, fetch_one
from app.llm.types import LLMError
from app.pipeline.router import enqueue

log = structlog.get_logger()

TRANSIENT_ALERT_AFTER = timedelta(minutes=30)   # and at least two failed turns in that time
TRANSIENT_ALERT_FAILURES = 2
REMIND_EVERY = timedelta(days=1)
DOWN, BACK = "model_down", "model_back"         # outbox dedupe-key prefixes

# What went wrong, as the family and the admin read it. Each finishes "I can't reply because ...".
CAUSES = {
    "key_rejected": "the AI service I use has stopped accepting my key",
    "key_missing": "no key for the AI service I use has been set up",
    "not_allowed": "my key for the AI service I use isn't allowed to do what I need",
    "out_of_credit": "the account for the AI service I use has run out of credit",
    "model_unknown": "the AI service I use doesn't know the model I'm set to use",
    "signed_out": "the subscription I use for AI has been signed out on the computer I run on",
    "cli_missing": "the program I use to reach the AI service isn't on the computer I run on",
    "unreachable": "the AI service I use isn't answering",
    "busy": "the AI service I use is turning me away as too busy",
    "usage_limit": "the AI subscription I use has reached its usage limit",
    "failed": "something is going wrong with the AI service I use",
}
FIX = ("Whoever looks after the computer I run on needs to fix it. The System page on the dashboard says "
       "what's wrong; send me the word dashboard for a link.")
STILL = "Reminders and the morning brief still go out, because they don't need it."
DOWN_TEXT = "I can't reply to anyone right now: {cause}. " + FIX + " " + STILL
DOWN_TRANSIENT_TEXT = ("I haven't been able to reply to anyone for the last {minutes} minutes: {cause}. It may sort "
                       "itself out; if it doesn't, the System page on the dashboard says more (send me the word "
                       "dashboard for a link). " + STILL)
REMINDER_TEXT = "I still can't reply to anyone, since {since}: {cause}. " + FIX
BACK_TEXT = "I'm answering again. Anything sent to me while I wasn't will need sending again."
# The reply to whoever wrote while the admins know.
TOLD_REPLY = "Sorry, I can't answer right now: the AI service I use has stopped working. I've let {admins} know."
ADMIN_REPLY = "Sorry, I can't answer right now: {cause}. The System page on the dashboard has more."


def cause_of(error: Exception) -> tuple[str, str, str | None]:
    """(kind, cause, the provider's name for it) of a failed turn. An error the adapter did not
    recognise counts as one that may pass."""
    if isinstance(error, LLMError) and error.cause:
        return error.kind or "transient", error.cause, error.error_type
    return "transient", "failed", getattr(error, "error_type", None)


async def _admins(conn: AsyncConnection) -> list[dict[str, Any]]:
    return await fetch_all(conn, "select id, household_id from members where is_admin "
                                 "order by household_id, created_at, id")


async def failed(conn: AsyncConnection, error: Exception, provider: str, now: datetime) -> dict[str, Any]:
    """Record a turn the model failed, and tell the admins if it is time. Returns the open outage."""
    kind, cause, error_type = cause_of(error)
    outage = await fetch_one(
        conn,
        """insert into model_outages (kind, cause, error_type, provider, started_at, last_failed_at)
           values (:kind, :cause, :error_type, :provider, :now, :now)
           on conflict ((true)) where ended_at is null do update set
             kind = excluded.kind, cause = excluded.cause, error_type = excluded.error_type,
             provider = excluded.provider, failures = model_outages.failures + 1,
             last_failed_at = greatest(model_outages.last_failed_at, excluded.last_failed_at)
           returning *""",
        kind=kind, cause=cause, error_type=(error_type or "")[:80] or None, provider=provider, now=now)
    assert outage is not None
    log.warning("model_failed", kind=kind, cause=cause, error_type=error_type, failures=outage["failures"])
    due = kind == "config" or (outage["failures"] >= TRANSIENT_ALERT_FAILURES
                               and now - outage["started_at"] >= TRANSIENT_ALERT_AFTER)
    if outage["alerted_at"] is None and due:
        claimed = await fetch_one(
            conn, "update model_outages set alerted_at = :now, reminded_at = :now "
                  "where id = :id and alerted_at is null returning alerted_at, reminded_at", id=outage["id"], now=now)
        if claimed:
            outage |= claimed
            text = (DOWN_TEXT if kind == "config" else DOWN_TRANSIENT_TEXT).format(
                cause=CAUSES[cause], minutes=int((now - outage["started_at"]).total_seconds() // 60))
            told = await _tell_admins(conn, f"{DOWN}:{outage['id']}", text, now)
            log.warning("model_outage_alerted", kind=kind, cause=cause, admins=told)
    elif outage["alerted_at"] is not None:
        claimed = await fetch_one(
            conn, "update model_outages set reminded_at = :now where id = :id and reminded_at <= :before "
                  "returning reminded_at", id=outage["id"], now=now, before=now - REMIND_EVERY)
        if claimed:
            outage |= claimed
            await _tell_admins(conn, f"{DOWN}:{outage['id']}:{now:%Y-%m-%d}", REMINDER_TEXT, now,
                               cause=CAUSES[cause], since=outage["started_at"])
            log.warning("model_outage_reminded", kind=kind, cause=cause)
    return outage


async def _tell_admins(conn: AsyncConnection, key: str, text: str, now: datetime, *,
                       cause: str | None = None, since: datetime | None = None) -> int:
    """One message to each admin of every household, in their own chat, under quiet hours: the
    family's messages are kept and its reminders still go, so nothing is lost by waiting till morning."""
    told = 0
    for admin in await _admins(conn):
        words = text
        if since is not None:
            timezone = await _timezone(conn, admin["household_id"])
            words = text.format(cause=cause, since=f"{local(since, timezone):%A %-d %B at %H:%M}")
        if await enqueue(conn, OutboundMessage(
                household_id=admin["household_id"], target="member", member_id=admin["id"], text=words,
                dedupe_key=f"{key}:{admin['id']}"), send_after=now):
            told += 1
    return told


async def _timezone(conn: AsyncConnection, household_id: str) -> str:
    row = await fetch_one(conn, "select timezone from households where id = :h", h=household_id)
    return str(row["timezone"]) if row else "UTC"


async def succeeded(conn: AsyncConnection, now: datetime) -> int:
    """A turn worked: close the open outage, if there is one. The admins who were told it was down
    are told it is back; a warning still held for quiet hours is dropped instead, unread. A short
    passing failure that never told anyone leaves no row. Returns how many messages were queued."""
    outage = await fetch_one(
        conn, "update model_outages set ended_at = :now where ended_at is null returning *", now=now)
    if outage is None:
        return 0
    log.info("model_recovered", kind=outage["kind"], cause=outage["cause"], failures=outage["failures"])
    if outage["alerted_at"] is None:
        if outage["kind"] == "transient":
            await execute(conn, "delete from model_outages where id = :id", id=outage["id"])
        return 0
    mine = f"{DOWN}:{outage['id']}:%"
    await execute(conn, "update outbox set status = 'cancelled', last_error = 'the model is answering again' "
                        "where status = 'pending' and dedupe_key like :mine", mine=mine)
    heard = await fetch_all(
        conn, "select distinct household_id, member_id from outbox where dedupe_key like :mine and status = 'sent' "
              "order by household_id, member_id", mine=mine)
    told = 0
    for admin in heard:
        if await enqueue(conn, OutboundMessage(
                household_id=admin["household_id"], target="member", member_id=admin["member_id"], text=BACK_TEXT,
                dedupe_key=f"{BACK}:{outage['id']}:{admin['member_id']}"), send_after=now):
            told += 1
    return told


async def reply_for(conn: AsyncConnection, outage: dict[str, Any] | None, household_id: str,
                    member_id: str | None, scope: str) -> str | None:
    """What to say to someone whose turn failed while the admins have been told; None for the
    usual apology. An admin in their own chat is told the cause itself."""
    if outage is None or outage["alerted_at"] is None:
        return None
    admins = await fetch_all(conn, "select id, name from members where household_id = :h and is_admin "
                                   "order by created_at, id", h=household_id)
    if not admins:
        return None
    if scope == "dm" and member_id in {a["id"] for a in admins}:
        return ADMIN_REPLY.format(cause=CAUSES[outage["cause"]])
    names = [a["name"] for a in admins]
    return TOLD_REPLY.format(admins=" and ".join([", ".join(names[:-1]), names[-1]] if len(names) > 1 else names))


async def health(conn: AsyncConnection, household_id: str, limit: int = 5) -> dict[str, Any]:
    """For the System page: the last turn the model answered here, the open outage, and the last few."""
    last = await fetch_one(
        conn, """select max(processed_at) as at from messages where household_id = :h and direction = 'in'
                 and status = 'processed' and meta ? 'turn'""", h=household_id)
    outages = await fetch_all(conn, "select * from model_outages order by started_at desc limit :n", n=limit + 1)
    current = next((o for o in outages if o["ended_at"] is None), None)
    for outage in outages:
        outage["said"] = CAUSES.get(outage["cause"], CAUSES["failed"])
    return {"last_answered": last["at"] if last else None, "current": current,
            "past": [o for o in outages if o["ended_at"] is not None][:limit]}
