"""Family tool: add_family_member."""
from typing import Literal

from pydantic import BaseModel

from app.agent.actions import record
from app.agent.base import Ctx
from app.config import get_settings
from app.core.envelope import OutboundMessage
from app.core.identity import invite_link
from app.db import fetch_val
from app.pipeline.router import enqueue
from app.services import members


class AddFamilyMember(BaseModel):
    name: str
    role: Literal["adult", "child"] = "child"


async def add_family_member(ctx: Ctx, args: AddFamilyMember) -> str:
    """Add a family member: a child, or another adult. For an adult who is not connected yet,
    an invite is sent separately to the person asking, for them to pass on."""
    async with record(ctx, "add_family_member", args) as rec:
        member_id, _ = await members.add_member(rec, args.name, args.role)
        role = await fetch_val(ctx.conn, "select role from members where id = :id", id=member_id)
        if role == "adult" and not await members.is_connected(ctx.conn, member_id):
            name = await fetch_val(ctx.conn, "select name from members where id = :id", id=member_id)
            if ctx.member_id is None:
                rec.lines.append(f"NOTE: invite {name} from the dashboard Family page")
            else:
                code = await members.invite(rec, member_id)
                await enqueue(ctx.conn, OutboundMessage(
                    household_id=ctx.household_id, target="member", member_id=ctx.member_id,
                    text=invite_text(name, code), respect_quiet_hours=False))
                rec.lines.append(f"NOTE: an invite for {name} was sent to this person in a separate message; "
                                 f"they pass it on and {name} connects by opening it")
    return rec.result


def invite_text(name: str, code: str) -> str:
    """The message an adult forwards to the person they are inviting. Written by code, so the
    invite code never passes through the model."""
    settings = get_settings()
    link = invite_link(code, settings.tg_bot_username)
    how = f"open {link} or send the code {code}" if link else f"send the code {code}"
    return (f"Invite for {name}. Pass this on: to connect to {settings.agent_name}, {how} "
            f"to {settings.agent_name} in a direct message. It works once per channel and expires in 7 days.")
