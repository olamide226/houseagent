"""Onboarding tool (spec section 12.2). Only offered while the household is being set up."""
from typing import Literal

from pydantic import BaseModel

from app.agent.actions import record
from app.agent.base import Ctx
from app.services import households


class OnboardingAdvance(BaseModel):
    step: Literal["family", "routines", "shops", "staples", "tour", "rhythm", "presence"]
    skipped: bool = False


async def onboarding_advance(ctx: Ctx, args: OnboardingAdvance) -> str:
    """Mark an onboarding step done or skipped. Returns the next step or 'complete'."""
    async with record(ctx, "onboarding_advance", args) as rec:
        await households.advance_onboarding(rec, args.step, args.skipped)
    return rec.result
