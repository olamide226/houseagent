"""Meal ideas tool: meal_ideas. The model proposes the meals; code gives it what there is to cook with."""
from pydantic import BaseModel

from app.agent.base import Ctx
from app.core.timeutil import local
from app.db import fetch_val
from app.services import households, inventory

# How to answer comes back with the food, not in the tool's description: every model step of every
# turn reads the description, and only a turn about meals reads this.
HOW_TO_ANSWER = (
    "NOTE: suggest 2 or 3 everyday meals (up to 5 for a week), numbered, one short line each: the meal, then what "
    "it still needs that is not listed above, or that they have everything for it. Build them on Use first, soonest "
    "date first, then on the rest; salt, pepper, oil and water are always there. Keep to every allergy, diet and "
    "dislike under Remembered, and to who the meal is for. Never use what is past its date: say that it is. No "
    "recipe unless asked. End by offering to put what is missing on the shopping list for the one they pick: that "
    "is all you can do with a meal, there is no meal plan to put it on."
)


class MealIdeas(BaseModel):
    pass


async def meal_ideas(ctx: Ctx, args: MealIdeas) -> str:
    """What there is to cook with, and how to answer. Call it whenever someone asks what to cook or eat:
    tonight, this week, with something they name, for the kids. When they then pick a meal you
    suggested, add exactly what you said it still needs with update_shopping_list and say what was
    added; if it needs nothing, add nothing and say so. Writes nothing itself."""
    timezone = await fetch_val(ctx.conn, "select timezone from households where id = :h", h=ctx.household_id)
    rows = await inventory.food_rows(ctx.conn, ctx.household_id)
    if not rows:
        return "OK: no food is recorded in the house yet"
    lines = [inventory.cooking_text(rows, local(ctx.now, timezone).date())]
    if facts := await households.facts(ctx.conn, ctx.household_id):
        # Every one of them: the brief stops at 25, and an allergy must not be the 26th.
        lines.append("Remembered: " + "; ".join(households.fact_text(fact) for fact in facts))
    return "\n".join([*lines, HOW_TO_ANSWER])
