"""Memory tool contract (spec section 9). Implemented and registered in milestone 3."""
from pydantic import BaseModel, Field

from app.agent.base import Ctx


class Remember(BaseModel):
    key: str = Field(description="snake_case, e.g. 'milk_brand', 'main_supermarket'")
    value: str | None = Field(None, description="None forgets the fact")
    about: str | None = Field(None, description="Member name, or omit for the whole household")


async def remember(ctx: Ctx, args: Remember) -> str:
    """Save or forget a durable household fact or preference."""
    raise NotImplementedError
