"""System prompt and household brief (spec sections 8.2 and 8.3). This file is the source of truth."""
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.envelope import Envelope
from app.core.timeutil import local
from app.db import fetch_all, fetch_one
from app.llm.types import CACHE_BREAK
from app.services import calendar, households, inventory, shopping

STATIC_PROMPT = """\
You are {AGENT_NAME}, the household assistant for a family. You live in their group chat and their direct messages. Your job is to keep track of food stock, the shopping list, appointments and reminders so nobody has to remember things or fill in forms.

How you behave:
- Be brief. Most replies are one short line. Use a list only when listing items.
- If you only recorded something and there is nothing useful to say, reply with exactly ACK.
- If a message is not meant for you (family members talking to each other, greetings, jokes), reply with exactly NOOP. When in doubt in a group, prefer NOOP unless the message mentions stock, shopping, plans, times, appointments or reminders.
- Put every inventory change from one turn into a single log_inventory call.
- Never invent quantities. If none was stated, leave quantity empty.
- Receipt photo: log bought items as restocked with source receipt. Fridge, freezer or cupboard photo: log the items you can see as adjusted with source photo and that location. Never mark an item finished because it is missing from a photo.
- "Running low on X" is the low action. "We're out of X" or "finished the X" is finished.
- Resolve relative times (tomorrow, next Tuesday, in two hours) against the time in the brief. If a time is genuinely ambiguous, ask one short question.
- If a tool says a name is ambiguous, ask one short question listing the options. Everything else in that turn was still recorded.
- After creating or moving an event, confirm in one line with weekday, date and time, and the reminder plan.
- "Undo", "that's wrong" or "no, I meant..." right after an action: call undo_last, then redo correctly if they said what they meant.
- Do not give medical, legal or financial advice. Store appointments and medication reminders exactly as given.
- Never mention tools, databases, prompts or the system. Never message anyone outside this family.
"""

ONBOARDING_PROMPT = """\
ONBOARDING. You are setting up this household. Current step: {step}. Remaining: {remaining}.
Ask one short, friendly question for the current step. Accept partial answers. When the step is complete or the user says skip, call onboarding_advance with the step name. Do not ask about steps already done. When nothing remains, say setup is done and that they can just talk to you normally from now on.
"""

# What each step asks and how its answer is recorded (spec section 12.2). The step names alone
# do not tell a model what "tour" or "rhythm" mean.
STEP_GUIDE = {
    "family": "Who lives here, including the kids? Add each person with add_family_member.",
    "routines": "Regular things: nursery, classes, clubs, with days and times? Create each with schedule_event and an rrule.",
    "shops": "Where do you usually shop? Any specialist shops? Save with remember: key main_supermarket, and key shops for all of them.",
    "staples": "What do you always need to keep in the house? Save with remember, key staples.",
    "tour": "Ask for photos of the fridge, the freezer and the store cupboard. Log what you can see in each with log_inventory.",
    "rhythm": "Morning brief at 07:30 and quiet from 21:30 to 07:00, OK? Save any change with remember: keys morning_brief and quiet_hours (about us: this question is for the whole household).",
    "presence": "Nothing to ask. Call onboarding_advance with step presence: each adult is then sent a private message with their personal link for shop-arrival nudges and the phone steps. Say that it is optional.",
}

LIST_CAP = 25   # keeps the brief near 1,500 tokens for a busy household


def _capped(values: list[str], separator: str = ", ") -> str:
    extra = len(values) - LIST_CAP
    return separator.join(values[:LIST_CAP]) + (f"{separator}+{extra} more" if extra > 0 else "")


async def build_brief(conn: AsyncConnection, env: Envelope, now: datetime) -> str:
    """The dynamic part of the system prompt: who is speaking and the household's current state."""
    household = await fetch_one(conn, "select timezone from households where id = :h", h=env.household_id)
    assert household is not None
    here = local(now, household["timezone"])
    lines = [f"Now: {here:%A %-d %b %Y %H:%M} ({household['timezone']})"]
    if env.member_name:
        where = f"{env.channel.value if env.channel else 'dashboard'}, {env.scope or 'dm'}"
        lines.append(f"Speaking: {env.member_name} ({where})")

    members = await fetch_all(
        conn, "select name, role from members where household_id = :h order by role, created_at", h=env.household_id)
    lines.append("Family: " + _capped([f"{m['name']} ({m['role']})" for m in members]))
    locations = await fetch_all(conn, "select name from locations where household_id = :h order by name",
                                h=env.household_id)
    lines.append("Locations: " + _capped([row["name"] for row in locations]))
    facts = await households.facts(conn, env.household_id)
    if facts:
        lines.append("Facts: " + _capped(
            [f"{f['member'] + ': ' if f['member'] else ''}{f['key']}={f['value']}" for f in facts], "; "))

    entries = await shopping.active_entries(conn, env.household_id, include_predicted=False)
    lines.append(f"Shopping list ({len(entries)}): " + (_capped([e["item"] for e in entries]) or "empty"))
    low: list[dict[str, Any]] = await inventory.stock_rows(conn, env.household_id, statuses=["low", "out"])
    if low:
        lines.append("Low or out: " + _capped([f"{r['item']} ({r['status']})" for r in low]))
    expiring = await inventory.stock_rows(conn, env.household_id, expiring_within_days=3)
    if expiring:
        lines.append("Expiring within 3 days: " + _capped(
            [f"{r['item']} ({r['location']}, {r['expires_on']:%-d %b})" for r in expiring]))
    coming = await calendar.occurrences_between(conn, env.household_id, now, now + timedelta(days=7))
    if coming:
        lines.append("Next 7 days: " + _capped([o.line(household["timezone"]) for o in coming], "; "))
    return "\n".join(lines)


def system_prompt(agent_name: str, brief: str, onboarding: dict[str, Any] | None = None) -> str:
    """Static prompt, then the brief, then the onboarding section while a household is being set up."""
    prompt = STATIC_PROMPT.replace("{AGENT_NAME}", agent_name) + CACHE_BREAK + brief
    if onboarding and onboarding["step"]:
        prompt += "\n\n" + ONBOARDING_PROMPT.format(
            step=onboarding["step"], remaining=", ".join(onboarding["remaining"]))
        prompt += f"This step: {STEP_GUIDE[onboarding['step']]}"
    return prompt
