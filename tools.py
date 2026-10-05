"""
Agent tool contract for the household agent (spec section 9).

Framework-agnostic: each tool is an async function plus a Pydantic args model.
  - LoopRuntime: tool_definitions() returns provider-neutral ToolDef dicts
    {name, description, parameters}; app/llm adapters translate them per vendor.
  - LettaRuntime: thin wrappers POST to /internal/tools/{name} with the same args.

Rules:
  1. Tools take natural names; resolve.py does alias, trigram and ambiguity handling.
  2. Batch-first: one turn, one call per tool where possible.
  3. Results are short text lines prefixed OK: / NEW: / AMBIGUOUS: / ERROR: / NOTE:.
  4. household_id and member_id come from Ctx (gateway-injected), never from the model.
  5. Deterministic side effects live in code (spec 9.4), not in the prompt.
  6. Every write records an inverse in agent_actions so undo_last can revert it.
  7. Tool functions are thin: resolve names, then call app/services/ (shared with the dashboard).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel, Field


class Ctx(BaseModel):
    """Injected by the pipeline. Invisible to the model."""
    household_id: str
    member_id: str | None            # None for system turns
    thread_id: str | None
    message_id: str | None           # last message of the debounced batch


class ToolError(Exception):
    """Domain error. Returned to the model as 'ERROR: <msg>' in a tool message with is_error=True."""


# ---------------------------------------------------------------- Inventory
class InventoryChange(BaseModel):
    item: str = Field(description="Natural name, e.g. 'eggs', 'Indomie', 'chicken thighs'")
    action: Literal["added", "used", "low", "finished", "restocked", "adjusted", "discarded"] = Field(
        description="low = running low; finished = none left; adjusted = absolute count seen (photos)")
    quantity: float | None = Field(None, description="Only if stated or clearly visible. Never guess.")
    unit: str | None = Field(None, description="e.g. 'pints', 'kg', 'packs'")
    location: str | None = Field(None, description="fridge, freezer, store, or a custom location. Omit for usual place.")
    expires_on: date | None = None


class LogInventory(BaseModel):
    changes: list[InventoryChange] = Field(min_length=1)
    source: Literal["message", "receipt", "photo"] = "message"


async def log_inventory(ctx: Ctx, args: LogInventory) -> str:
    """Record food and household stock that came in, got used, is running low or ran out.
    Batch every change from the turn into one call. Unknown items are created (NEW:).
    Ambiguous names are skipped and reported (AMBIGUOUS:); other changes still apply.
    Finished staples and anything running low are added to the shopping list (NOTE:)."""
    ...


class QueryInventory(BaseModel):
    item: str | None = None
    location: str | None = None
    status: list[Literal["in_stock", "low", "out", "unknown"]] | None = None
    expiring_within_days: int | None = Field(None, ge=0, le=60)


async def query_inventory(ctx: Ctx, args: QueryInventory) -> str:
    """Answer 'do we have X', 'what's in the freezer', 'what's running low', 'what expires soon'."""
    ...


# ---------------------------------------------------------------- Shopping
class ShoppingAdd(BaseModel):
    item: str
    quantity: float | None = None
    unit: str | None = None
    store_hint: str | None = Field(None, description="e.g. 'African shop', 'Costco'")


class UpdateShoppingList(BaseModel):
    add: list[ShoppingAdd] = []
    bought: list[str] = Field([], description="Ticks off AND logs a restock to inventory")
    remove: list[str] = Field([], description="No longer needed (dismissed)")
    bought_all: bool = Field(False, description="True for 'got everything on the list'")


async def update_shopping_list(ctx: Ctx, args: UpdateShoppingList) -> str:
    """Add, tick off, or remove items on the shared shopping list."""
    ...


class GetShoppingList(BaseModel):
    store: str | None = Field(None, description="Filter to one shop")
    include_predicted: bool = Field(True, description="Include items probably running low")


async def get_shopping_list(ctx: Ctx, args: GetShoppingList) -> str:
    """The current list grouped by category; explicit items first, predicted marked (probably)."""
    ...


# ---------------------------------------------------------------- Calendar
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
    ...


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
    ...


class ListUpcoming(BaseModel):
    days: int = Field(7, ge=1, le=60)
    member: str | None = None


async def list_upcoming(ctx: Ctx, args: ListUpcoming) -> str:
    """What's coming up, optionally for one person. Includes standalone reminders."""
    ...


class SetReminder(BaseModel):
    text: str
    fire_at: datetime | None = Field(None, description="One-off time")
    rrule: str | None = Field(None, description="Repeating, e.g. FREQ=WEEKLY;BYDAY=SU;BYHOUR=18")
    target: str = Field("me", description="'me', 'household', or a member name")
    urgent: bool = Field(False, description="True only if the user wants it even during quiet hours")


async def set_reminder(ctx: Ctx, args: SetReminder) -> str:
    """A reminder not tied to an event ('remind me to call the landlord Friday at 9')."""
    ...


# ---------------------------------------------------------------- Memory and family
class Remember(BaseModel):
    key: str = Field(description="snake_case, e.g. 'milk_brand', 'main_supermarket'")
    value: str | None = Field(None, description="None forgets the fact")
    about: str | None = Field(None, description="Member name, or omit for the whole household")


async def remember(ctx: Ctx, args: Remember) -> str:
    """Save or forget a durable household fact or preference."""
    ...


class AddFamilyMember(BaseModel):
    name: str
    role: Literal["adult", "child"] = "child"


async def add_family_member(ctx: Ctx, args: AddFamilyMember) -> str:
    """Add a family member record (kids, or an adult who will be invited separately)."""
    ...


# ---------------------------------------------------------------- Undo and onboarding
class UndoLast(BaseModel):
    n: int = Field(1, ge=1, le=5)


async def undo_last(ctx: Ctx, args: UndoLast) -> str:
    """Revert this person's last n actions from the past 24 hours, including stock and list."""
    ...


class OnboardingAdvance(BaseModel):
    step: Literal["family", "routines", "shops", "staples", "tour", "rhythm", "presence"]
    skipped: bool = False


async def onboarding_advance(ctx: Ctx, args: OnboardingAdvance) -> str:
    """Mark an onboarding step done or skipped. Returns the next step or 'complete'."""
    ...


# ---------------------------------------------------------------- Registry
@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[[Ctx, BaseModel], Awaitable[str]]
    args: type[BaseModel]
    onboarding_only: bool = False


REGISTRY: dict[str, ToolSpec] = {
    t.name: t
    for t in [
        ToolSpec("log_inventory", log_inventory, LogInventory),
        ToolSpec("query_inventory", query_inventory, QueryInventory),
        ToolSpec("update_shopping_list", update_shopping_list, UpdateShoppingList),
        ToolSpec("get_shopping_list", get_shopping_list, GetShoppingList),
        ToolSpec("schedule_event", schedule_event, ScheduleEvent),
        ToolSpec("modify_event", modify_event, ModifyEvent),
        ToolSpec("list_upcoming", list_upcoming, ListUpcoming),
        ToolSpec("set_reminder", set_reminder, SetReminder),
        ToolSpec("remember", remember, Remember),
        ToolSpec("add_family_member", add_family_member, AddFamilyMember),
        ToolSpec("undo_last", undo_last, UndoLast),
        ToolSpec("onboarding_advance", onboarding_advance, OnboardingAdvance, onboarding_only=True),
    ]
}


def tool_definitions(onboarding_active: bool) -> list[dict]:
    """Provider-neutral tool definitions (app.llm.types.ToolDef shape).
    The openai_compat adapter wraps each as {type: "function", function: {...}};
    the anthropic adapter renames parameters to input_schema."""
    return [
        {
            "name": spec.name,
            "description": (spec.fn.__doc__ or "").strip(),
            "parameters": spec.args.model_json_schema(),
        }
        for spec in REGISTRY.values()
        if onboarding_active or not spec.onboarding_only
    ]
