"""Agent tool contract (spec section 9).

Each tool is an async function plus a Pydantic args model.
  1. Tools take natural names; resolve.py does alias, trigram and ambiguity handling.
  2. Batch-first: one turn, one call per tool where possible.
  3. Results are short text lines prefixed OK: / NEW: / AMBIGUOUS: / ERROR: / NOTE:.
  4. household_id and member_id come from Ctx (gateway-injected), never from the model.
  5. Deterministic side effects live in code (spec 9.4), not in the prompt.
  6. Every write records an inverse in agent_actions so undo_last can revert it.
  7. Tool functions are thin: resolve names, then call app/services/ (shared with the dashboard).

REGISTRY holds all twelve; `onboarding_advance` is only offered while a household is being set up.
"""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from app.agent.base import Ctx, ToolError
from app.agent.tools import calendar, family, inventory, memory, onboarding, shopping, undo
from app.llm.types import ToolDef

log = structlog.get_logger()


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[[Ctx, Any], Awaitable[str]]
    args: type[BaseModel]
    onboarding_only: bool = False


REGISTRY: dict[str, ToolSpec] = {
    t.name: t
    for t in [
        ToolSpec("log_inventory", inventory.log_inventory, inventory.LogInventory),
        ToolSpec("query_inventory", inventory.query_inventory, inventory.QueryInventory),
        ToolSpec("update_shopping_list", shopping.update_shopping_list, shopping.UpdateShoppingList),
        ToolSpec("get_shopping_list", shopping.get_shopping_list, shopping.GetShoppingList),
        ToolSpec("schedule_event", calendar.schedule_event, calendar.ScheduleEvent),
        ToolSpec("modify_event", calendar.modify_event, calendar.ModifyEvent),
        ToolSpec("list_upcoming", calendar.list_upcoming, calendar.ListUpcoming),
        ToolSpec("set_reminder", calendar.set_reminder, calendar.SetReminder),
        ToolSpec("remember", memory.remember, memory.Remember),
        ToolSpec("add_family_member", family.add_family_member, family.AddFamilyMember),
        ToolSpec("undo_last", undo.undo_last, undo.UndoLast),
        ToolSpec("onboarding_advance", onboarding.onboarding_advance, onboarding.OnboardingAdvance,
                 onboarding_only=True),
    ]
}


def tool_definitions(onboarding_active: bool) -> list[ToolDef]:
    """Provider-neutral tool definitions; app/llm adapters translate them per vendor."""
    return [
        ToolDef(name=spec.name, description=(spec.fn.__doc__ or "").strip(),
                parameters=spec.args.model_json_schema())
        for spec in REGISTRY.values()
        if onboarding_active or not spec.onboarding_only
    ]


async def run_tool(name: str, raw_args: dict[str, Any], ctx: Ctx) -> tuple[str, bool]:
    """Validate and run one tool call in its own savepoint. Returns (result text, is_error).

    A failed call rolls back only its own writes; earlier calls in the turn stand."""
    spec = REGISTRY.get(name)
    if spec is None:
        return f"ERROR: unknown tool {name}", True
    try:
        args = spec.args.model_validate(raw_args)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return f"ERROR: invalid arguments ({problems})", True
    if (name, args.model_dump(mode="json")) in ctx.undone:
        # Models sometimes follow an undo by repeating the very call it reverted.
        return "OK: not done again: that is exactly what was just undone", False
    try:
        async with ctx.conn.begin_nested():
            return await spec.fn(ctx, args), False
    except ToolError as exc:
        return f"ERROR: {exc}", True
    except Exception:
        log.exception("tool_failed", tool=name, household_id=ctx.household_id, message_id=ctx.message_id)
        return "ERROR: that did not work, nothing was recorded for this step", True
