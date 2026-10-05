"""Undo tool."""
from pydantic import BaseModel, Field

from app.agent import actions
from app.agent.base import Ctx


class UndoLast(BaseModel):
    n: int = Field(1, ge=1, le=5)


async def undo_last(ctx: Ctx, args: UndoLast) -> str:
    """Revert this person's last n actions from the past 24 hours, including stock and list."""
    return "\n".join(await actions.undo_last(ctx, args.n))
