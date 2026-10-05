"""Onboarding tool contract (spec section 12.2). Implemented and registered in milestone 3."""
from typing import Literal

from pydantic import BaseModel

from app.agent.base import Ctx


class OnboardingAdvance(BaseModel):
    step: Literal["family", "routines", "shops", "staples", "tour", "rhythm", "presence"]
    skipped: bool = False


async def onboarding_advance(ctx: Ctx, args: OnboardingAdvance) -> str:
    """Mark an onboarding step done or skipped. Returns the next step or 'complete'."""
    raise NotImplementedError
