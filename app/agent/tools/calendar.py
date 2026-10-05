"""Calendar tool contract (spec section 9). Implemented and registered in milestone 2."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.agent.base import Ctx


class ScheduleEvent(BaseModel):
    title: str
    kind: Literal["appointment", "activity", "task"] = "appointment"
    starts_at: datetime = Field(description="ISO 8601 with offset; naive means household time")
    ends_at: datetime | None = None
    rrule: str | None = Field(None, description="RFC 5545, e.g. FREQ=WEEKLY;BYDAY=TU,TH")
    participants: list[str] = Field([], description="Names, 'me', 'us', or 'the kids'")
    location: str | None = None
    remind_before_minutes: list[int] = Field([1440, 60], description="Default: day before and 1h before")
    notes: str | None = None


async def schedule_event(ctx: Ctx, args: ScheduleEvent) -> str:
    """Create an appointment or recurring activity, with reminders. Appears in the ICS feed."""
    raise NotImplementedError


class ModifyEvent(BaseModel):
    event: str = Field(description="How the user refers to it, e.g. 'Ada's GP appointment'")
    cancel: bool = False
    scope: Literal["this", "all"] = Field("this", description="For recurring events")
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    title: str | None = None
    location: str | None = None
    participants: list[str] | None = None
    notes: str | None = None


async def modify_event(ctx: Ctx, args: ModifyEvent) -> str:
    """Move, edit or cancel an event. Reminders are regenerated automatically."""
    raise NotImplementedError


class ListUpcoming(BaseModel):
    days: int = Field(7, ge=1, le=60)
    member: str | None = None


async def list_upcoming(ctx: Ctx, args: ListUpcoming) -> str:
    """What's coming up, optionally for one person. Includes standalone reminders."""
    raise NotImplementedError


class SetReminder(BaseModel):
    text: str
    fire_at: datetime | None = Field(None, description="One-off time")
    rrule: str | None = Field(None, description="Repeating, e.g. FREQ=WEEKLY;BYDAY=SU;BYHOUR=18")
    target: str = Field("me", description="'me', 'household', or a member name")
    urgent: bool = Field(False, description="True only if the user wants it even during quiet hours")


async def set_reminder(ctx: Ctx, args: SetReminder) -> str:
    """A reminder not tied to an event ('remind me to call the landlord Friday at 9')."""
    raise NotImplementedError
