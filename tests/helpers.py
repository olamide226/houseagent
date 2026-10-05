"""Seed helpers and fakes shared by the tests."""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncConnection

from app.agent.base import Ctx
from app.db import execute, fetch_all, fetch_val
from app.llm.types import ChatMessage, LLMResponse, ToolCall, ToolDef, Usage
from app.services import households, inventory


@dataclass
class Home:
    id: str
    ola: str                      # admin adult
    members: dict[str, str] = field(default_factory=dict)


async def seed_home(conn: AsyncConnection, *, telegram_id: str | None = "1001") -> Home:
    household_id, admin_id = await households.create_household(conn, "Adebayo", "Europe/London", "Ola")
    home = Home(household_id, admin_id, {"Ola": admin_id})
    if telegram_id:
        await link(conn, admin_id, telegram_id)
    return home


async def add_member(conn: AsyncConnection, home: Home, name: str, *, role: str = "adult",
                     telegram_id: str | None = None) -> str:
    member_id = str(await fetch_val(
        conn, "insert into members (household_id, name, role) values (:h, :name, :role) returning id",
        h=home.id, name=name, role=role,
    ))
    home.members[name] = member_id
    if telegram_id:
        await link(conn, member_id, telegram_id)
    return member_id


async def link(conn: AsyncConnection, member_id: str, telegram_id: str) -> None:
    await execute(conn, "insert into channel_identities (member_id, channel, handle) "
                        "values (:m, 'telegram', :handle)", m=member_id, handle=telegram_id)
    await execute(conn, "update members set preferred_channel = 'telegram' where id = :m", m=member_id)


async def add_item(conn: AsyncConnection, home: Home, name: str, *, location: str = "store",
                   staple: bool = False, qty: float | None = None, status: str | None = None,
                   threshold: float | None = None, aliases: list[str] | None = None) -> str:
    """An item, with a stock row when `qty` or `status` is given."""
    location_id = await location_id_of(conn, home, location)
    item_id = await inventory.create_item(conn, home.id, name, location_id, is_staple=staple)
    await execute(conn, "update items set low_threshold = :t, aliases = :aliases where id = :id",
                  t=threshold, aliases=aliases or [], id=item_id)
    if qty is not None or status is not None:
        await execute(
            conn,
            "insert into stock (item_id, location_id, qty_estimate, status, last_event_at) "
            "values (:i, :l, :qty, :status, now() - interval '1 day')",
            i=item_id, l=location_id, qty=None if qty is None else Decimal(str(qty)),
            status=status or "in_stock",
        )
    return item_id


async def location_id_of(conn: AsyncConnection, home: Home, name: str) -> str:
    return str(await fetch_val(conn, "select id from locations where household_id = :h and name = :n",
                               h=home.id, n=name))


def ctx_for(conn: AsyncConnection, home: Home, member: str = "Ola", **kwargs: Any) -> Ctx:
    return Ctx(conn=conn, household_id=home.id, member_id=home.members[member], **kwargs)


async def stock_of(conn: AsyncConnection, home: Home) -> dict[tuple[str, str], tuple[Any, str]]:
    rows = await fetch_all(
        conn,
        """select i.canonical_name as item, l.name as location, s.qty_estimate, s.status
           from stock s join items i on i.id = s.item_id join locations l on l.id = s.location_id
           where i.household_id = :h""", h=home.id,
    )
    return {(r["item"], r["location"]): (r["qty_estimate"], r["status"]) for r in rows}


async def stock_snapshot(conn: AsyncConnection, home: Home) -> list[dict[str, Any]]:
    return await fetch_all(
        conn,
        """select s.item_id, s.location_id, s.qty_estimate, s.status, s.expires_on, s.last_event_at
           from stock s join items i on i.id = s.item_id where i.household_id = :h
           order by s.item_id, s.location_id""", h=home.id,
    )


async def list_snapshot(conn: AsyncConnection, home: Home) -> list[dict[str, Any]]:
    return await fetch_all(
        conn, "select * from shopping_list_items where household_id = :h order by id", h=home.id)


async def active_list(conn: AsyncConnection, home: Home) -> dict[str, str]:
    rows = await fetch_all(
        conn,
        """select i.canonical_name as item, s.reason from shopping_list_items s
           join items i on i.id = s.item_id where s.household_id = :h and s.status = 'needed'""", h=home.id,
    )
    return {r["item"]: r["reason"] for r in rows}


async def events_of(conn: AsyncConnection, home: Home) -> list[tuple[str, str, Any, str]]:
    rows = await fetch_all(
        conn,
        """select i.canonical_name as item, e.event_type, e.quantity, e.source from inventory_events e
           join items i on i.id = e.item_id where e.household_id = :h order by e.occurred_at, e.id""",
        h=home.id,
    )
    return [(r["item"], r["event_type"], r["quantity"], r["source"]) for r in rows]


def say(text: str) -> LLMResponse:
    return LLMResponse(text=text, stop="end", usage=Usage(input_tokens=100, output_tokens=5))


def call(name: str, **arguments: Any) -> LLMResponse:
    return LLMResponse(
        text=None, stop="tool_calls", usage=Usage(input_tokens=100, output_tokens=20),
        tool_calls=[ToolCall(id=f"call_{name}", name=name, arguments=arguments)],
    )


class FakeLLM:
    """Plays back scripted responses and records what the runtime sent."""

    def __init__(self, *script: LLMResponse, supports_images: bool = True) -> None:
        self.script = list(script)
        self.supports_images = supports_images
        self.requests: list[tuple[str, list[ChatMessage], list[ToolDef]]] = []

    async def complete(self, system: str, messages: list[ChatMessage], tools: list[ToolDef],
                       max_tokens: int = 1024, temperature: float = 0.2) -> LLMResponse:
        self.requests.append((system, [m.model_copy(deep=True) for m in messages], tools))
        if not self.script:
            raise AssertionError("FakeLLM ran out of scripted responses")
        return self.script.pop(0)
