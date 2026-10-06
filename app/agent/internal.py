"""The tool bridge for an external agent runtime (spec section 8.4): `POST /internal/tools/{name}`.

Letta runs the model and calls each tool over HTTP. The call is run on the connection of the
household's turn in flight, by the process that is running that turn. A tool called through
Letta therefore writes exactly what the same call writes in the loop: same transaction, same
undo log, same outbox. Which turn is meant is unambiguous, because turns are serialised per
household. See ADR 0029.
"""
import asyncio
import hmac
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.agent.base import Ctx, ToolCallRecord
from app.agent.tools import REGISTRY, run_tool
from app.config import get_settings


@dataclass
class Turn:
    """One household's turn in flight in this process, and the tool calls made in it so far."""
    ctx: Ctx
    onboarding: bool
    records: list[ToolCallRecord] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)   # one call at a time on the turn's connection


IN_FLIGHT: dict[str, Turn] = {}


@contextmanager
def turn_in_flight(ctx: Ctx, onboarding: bool) -> Iterator[Turn]:
    """Tool calls for this household reach `ctx` until the block ends."""
    turn = IN_FLIGHT[ctx.household_id] = Turn(ctx, onboarding)
    try:
        yield turn
    finally:
        if IN_FLIGHT.get(ctx.household_id) is turn:
            del IN_FLIGHT[ctx.household_id]


class ToolCallIn(BaseModel):
    household_id: str
    args: dict[str, Any] = {}


class ToolCallOut(BaseModel):
    result: str
    is_error: bool


def _authorised(authorization: str = Header("")) -> None:
    token = get_settings().internal_tool_token
    if not token:
        raise HTTPException(status_code=404)   # no token configured: there is no bridge
    if not hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode()):
        raise HTTPException(status_code=401)


router = APIRouter(prefix="/internal", dependencies=[Depends(_authorised)])


@router.post("/tools/{name}")
async def call_tool(name: str, call: ToolCallIn) -> ToolCallOut:
    turn = IN_FLIGHT.get(call.household_id)
    if turn is None:
        # A call that arrives after its turn ended (a timeout, a retry) must not write anything.
        raise HTTPException(status_code=409, detail="no turn in progress for this household")
    async with turn.lock:
        spec = REGISTRY.get(name)
        if spec is not None and spec.onboarding_only and not turn.onboarding:
            result, is_error = f"ERROR: unknown tool {name}", True   # as in the loop, where it is not offered
        else:
            result, is_error = await run_tool(name, call.args, turn.ctx)
        turn.records.append(ToolCallRecord(name=name, args=call.args, result=result, is_error=is_error))
    return ToolCallOut(result=result, is_error=is_error)
