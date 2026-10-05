"""Agent runtime interface (spec section 8) and the context injected into every tool."""
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.envelope import Envelope
from app.core.timeutil import utcnow
from app.llm.types import Usage


class Ctx(BaseModel):
    """Injected by the pipeline or the dashboard. Invisible to the model."""
    model_config = ConfigDict(arbitrary_types_allowed=True)

    conn: AsyncConnection            # the turn's transaction; every write goes through it
    household_id: str
    member_id: str | None            # None for system turns
    thread_id: str | None = None
    message_id: str | None = None    # last message of the debounced batch
    source: Literal["agent", "dashboard"] = "agent"
    now: datetime = Field(default_factory=utcnow)   # when the turn's message arrived; "tomorrow" counts from here


class ToolError(Exception):
    """Domain error. Returned to the model as 'ERROR: <msg>' in a tool message with is_error=True."""


class ToolCallRecord(BaseModel):
    name: str
    args: dict[str, Any]
    result: str
    is_error: bool


class AgentResult(BaseModel):
    reply: str | None
    ack_only: bool = False      # model answered exactly ACK
    noop: bool = False          # model answered exactly NOOP
    tool_calls: list[ToolCallRecord] = []
    usage: Usage = Usage()


class AgentRuntime(Protocol):
    async def handle(self, env: Envelope, ctx: Ctx) -> AgentResult: ...
