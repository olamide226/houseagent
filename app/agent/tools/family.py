"""Family tool contract (spec section 9). Implemented and registered in milestone 3."""
from typing import Literal

from pydantic import BaseModel

from app.agent.base import Ctx


class AddFamilyMember(BaseModel):
    name: str
    role: Literal["adult", "child"] = "child"


async def add_family_member(ctx: Ctx, args: AddFamilyMember) -> str:
    """Add a family member record (kids, or an adult who will be invited separately)."""
    raise NotImplementedError
