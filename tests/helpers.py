"""Seed helpers and fakes shared by the tests."""
import base64
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
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


async def seed_home(conn: AsyncConnection, *, telegram_id: str | None = "1001", onboarding: bool = False) -> Home:
    """A household with its admin, Ola. Setup is already complete unless `onboarding` is set."""
    household_id, admin_id = await households.create_household(conn, "Adebayo", "Europe/London", "Ola")
    if not onboarding:
        await execute(conn, """update households set onboarding_state = '{"step": null, "done": []}' """
                            "where id = :h", h=household_id)
    home = Home(household_id, admin_id, {"Ola": admin_id})
    if telegram_id:
        await link(conn, admin_id, telegram_id)
    return home


async def add_member(conn: AsyncConnection, home: Home, name: str, *, role: str = "adult",
                     telegram_id: str | None = None, whatsapp_id: str | None = None,
                     imessage_id: str | None = None) -> str:
    member_id = str(await fetch_val(
        conn, "insert into members (household_id, name, role) values (:h, :name, :role) returning id",
        h=home.id, name=name, role=role,
    ))
    home.members[name] = member_id
    if telegram_id:
        await link(conn, member_id, telegram_id)
    if whatsapp_id:
        await link(conn, member_id, whatsapp_id, "whatsapp")
    if imessage_id:
        await link(conn, member_id, imessage_id, "imessage")
    return member_id


async def link(conn: AsyncConnection, member_id: str, handle: str, channel: str = "telegram",
               verified_at: Any = None) -> None:
    """Connect a handle. The first channel linked becomes the preferred one, as when an invite is redeemed."""
    await execute(conn, "insert into channel_identities (member_id, channel, handle, verified_at) "
                        "values (:m, :channel, :handle, coalesce(cast(:at as timestamptz), now()))",
                  m=member_id, channel=channel, handle=handle, at=verified_at)
    await execute(conn, "update members set preferred_channel = coalesce(preferred_channel, :channel) "
                        "where id = :m", m=member_id, channel=channel)


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


def call(tool: str, /, **arguments: Any) -> LLMResponse:
    return LLMResponse(
        text=None, stop="tool_calls", usage=Usage(input_tokens=100, output_tokens=20),
        tool_calls=[ToolCall(id=f"call_{tool}", name=tool, arguments=arguments)],
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


class FakeAdapter:
    """A channel that records sends instead of making them."""

    def __init__(self, channel: Any = None, *, reactions: bool = True, fail: Exception | None = None,
                 window_hours: int | None = None, template: str | None = None) -> None:
        from app.core.envelope import Capabilities, Channel

        self.channel = channel or Channel.telegram
        self.capabilities = Capabilities(
            groups=True, reactions=reactions, ack_emoji="\U0001F44D", voice_in=True, images_in=True,
            threaded_replies=True, proactive_window_hours=window_hours, proactive_template=template,
            max_text_len=4096, formatting="plain")
        self.fail = fail
        self.healthy = True     # what `ping` answers
        self.degraded = False   # what the health check last found
        self.sent: list[tuple[str, str, str | None]] = []       # (chat, text, reply_to)
        self.templates: list[tuple[str, str, list[str]]] = []   # (chat, template, params)
        self.reactions: list[tuple[str, str, str]] = []         # (chat, message, emoji)
        self.fetched: list[str] = []                            # external ids downloaded

    async def send_text(self, external_thread_id: str, text: str, reply_to_external_id: str | None = None) -> Any:
        from app.core.envelope import SendResult

        if self.fail:
            raise self.fail
        self.sent.append((external_thread_id, text, reply_to_external_id))
        return SendResult(external_id=f"out-{len(self.sent)}")

    async def send_template(self, external_thread_id: str, name: str, params: list[str]) -> Any:
        from app.core.envelope import SendResult

        if self.fail:
            raise self.fail
        self.templates.append((external_thread_id, name, params))
        return SendResult(external_id=f"template-{len(self.templates)}")

    async def react(self, external_thread_id: str, external_message_id: str, emoji: str) -> None:
        from app.channels.base import NotSupported

        if not self.capabilities.reactions:
            raise NotSupported
        if self.fail:
            raise self.fail
        self.reactions.append((external_thread_id, external_message_id, emoji))

    async def fetch_media(self, ref: Any) -> tuple[bytes, str]:
        if self.fail:
            raise self.fail
        self.fetched.append(ref.external_id)
        if ref.kind == "image":
            return f"image:{ref.external_id}".encode(), ref.mime or "image/jpeg"
        return b"audio-bytes", ref.mime or "audio/ogg"

    def dm_thread_id(self, handle: str) -> str:
        return handle

    def format(self, text: str) -> str:
        return text

    async def ping(self) -> bool:
        return self.healthy


class MemoryStore:
    """A MediaStore that keeps objects in a dict, named like the S3 backend names them."""
    backend = "s3"

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.fail = fail

    async def put(self, household_id: str, message_id: str, n: int, data: bytes, mime: str) -> Any:
        from app.core.envelope import MediaRef
        from app.media.store import EXTENSIONS, kind_of

        if self.fail:
            raise self.fail
        key = f"{household_id}/{message_id}/{n}.{EXTENSIONS.get(mime, 'bin')}"
        self.objects[key] = data
        return MediaRef(kind=kind_of(mime), mime=mime, storage_backend="s3", storage_key=key)

    async def get(self, ref: Any) -> bytes:
        return self.objects[ref.storage_key]

    async def delete(self, ref: Any) -> None:
        if self.fail:
            raise self.fail
        self.deleted.append(ref.storage_key)
        self.objects.pop(ref.storage_key, None)


def tg_update(update_id: int, text: str | None = None, *, user_id: int = 1001, name: str = "Ola",
              chat_id: int | None = None, chat_type: str = "private", **message: Any) -> dict[str, Any]:
    """A Telegram webhook update for a text (or other) message."""
    body: dict[str, Any] = {
        "message_id": update_id, "date": 1791230400,
        "from": {"id": user_id, "is_bot": False, "first_name": name},
        "chat": {"id": chat_id if chat_id is not None else user_id, "type": chat_type},
        **message,
    }
    if text is not None:
        body["text"] = text
    return {"update_id": update_id, "message": body}


WA_SECRET = "test-app-secret"
WA_BUSINESS = "447700900100"


def wa_message(n: int, text: str | None = None, *, user_id: str = "GB.1000000000000000000101",
               phone: str | None = "447700900101", name: str = "Ola", group_id: str | None = None,
               **message: Any) -> dict[str, Any]:
    """A WhatsApp `messages` webhook carrying one text (or other) message."""
    body: dict[str, Any] = {"from_user_id": user_id, "id": f"wamid.test{n:04d}", "timestamp": "1791230400",
                            "type": "text", **message}
    contact: dict[str, Any] = {"profile": {"name": name}, "user_id": user_id}
    if phone:
        body["from"] = contact["wa_id"] = phone
    if group_id:
        body["group_id"] = group_id
    if text is not None:
        body["text"] = {"body": text}
    return wa_webhook("messages", contacts=[contact], messages=[body])


def wa_webhook(field: str, **value: Any) -> dict[str, Any]:
    metadata = {"display_phone_number": WA_BUSINESS, "phone_number_id": "100000000000001"}
    return {"object": "whatsapp_business_account", "entry": [{"id": "200000000000002", "changes": [
        {"value": {"messaging_product": "whatsapp", "metadata": metadata, **value}, "field": field}]}]}


async def post_whatsapp(client: Any, payload: dict[str, Any]) -> Any:
    """Post a webhook the way Meta does: the raw body signed with the app secret."""
    import hashlib
    import hmac
    import json

    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(WA_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return await client.post("/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": signature})


BB_SECRET = "test-bb-secret"


def bb_message(n: int, text: str | None = None, *, handle: str = "+447700900101", group: str | None = None,
               kind: str = "new-message", **data: Any) -> dict[str, Any]:
    """A BlueBubbles webhook carrying one message, in the notification form the server sends."""
    chat = ({"guid": group, "style": 43, "displayName": "Adebayo family"} if group
            else {"guid": f"iMessage;-;{handle}", "style": 45, "displayName": ""})
    return {"type": kind, "data": {
        "guid": f"5C0FFEE0-{n:04d}", "text": text, "handle": {"address": handle, "service": "iMessage"},
        "attachments": [], "error": 0, "dateCreated": 1791230400000 + n, "isFromMe": False,
        "associatedMessageGuid": None, "associatedMessageType": None, "threadOriginatorGuid": None,
        "chats": [chat], **data}}


async def post_imessage(client: Any, payload: dict[str, Any], secret: str | None = BB_SECRET) -> Any:
    """Post a webhook the way BlueBubbles does: unsigned, the secret in the URL it was given."""
    return await client.post("/webhooks/imessage", json=payload, params={"secret": secret} if secret else None)


def london(text: str) -> Any:
    """A UTC instant from London wall-clock time, e.g. london("2026-10-27 09:00")."""
    from datetime import datetime

    from app.core.timeutil import to_utc

    return to_utc(datetime.fromisoformat(text), "Europe/London")


def wall(moment: Any) -> str:
    """London wall-clock time of an instant, e.g. "Tue 27 Oct 09:00"."""
    from app.core.timeutil import local

    return f"{local(moment, 'Europe/London'):%a %-d %b %H:%M}"


async def history(conn: AsyncConnection, home: Home, item_id: str, *events: tuple[str, Any]) -> None:
    """Inventory events at given instants, or London wall-clock times such as ("restocked", "2026-09-14 09:00").
    Stock is not touched."""
    for kind, at in events:
        await execute(
            conn,
            """insert into inventory_events (household_id, item_id, location_id, event_type, source, occurred_at)
               values (:h, :item, (select default_location_id from items where id = :item), :kind, 'message', :at)""",
            h=home.id, item=item_id, kind=kind, at=london(at) if isinstance(at, str) else at)


async def bought_every(conn: AsyncConnection, home: Home, name: str, days: int, last: str, *,
                       staple: bool = False) -> str:
    """An item with two full cycles of `days` days behind it, last restocked at `last` (London time) and not
    yet run out: the model predicts it runs out `days` days after `last`."""
    from datetime import timedelta

    item_id = await add_item(conn, home, name, staple=staple)
    start = london(last) - timedelta(days=2 * days + 2)
    await history(conn, home, item_id, *(
        (kind, start + timedelta(days=offset)) for kind, offset in
        (("restocked", 0), ("finished", days), ("restocked", days + 1), ("finished", 2 * days + 1))),
        ("restocked", last))
    return item_id


# A stand-in for the `claude` and `codex` CLIs (the `cli` fixture puts it on PATH under both
# names). It records how it was run, then prints and exits as the test planned.
STAND_IN = '''#!{python}
import base64, json, os, sys, time
home = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ["CODEX_HOME"]
count = len([name for name in os.listdir(home) if name.startswith("seen-")])
plans = json.load(open(os.path.join(home, "plans.json")))
plan = plans[min(count, len(plans) - 1)]
files = {{name: base64.b64encode(open(name, "rb").read()).decode() for name in os.listdir(".")}}
with open(os.path.join(home, "seen-%d.json" % count), "w") as seen:
    json.dump({{"argv": sys.argv[1:], "stdin": sys.stdin.read(), "env": dict(os.environ), "cwd": os.getcwd(),
               "files": files, "pid": os.getpid()}}, seen)
time.sleep(plan.get("sleep", 0))
sys.stdout.write(plan.get("stdout", ""))
sys.stderr.write(plan.get("stderr", ""))
sys.exit(plan.get("exit", 0))
'''


class StandIn:
    """Plans what the stand-in CLI answers and reads back how it was run."""

    def __init__(self, home: Path) -> None:
        self.home = home

    def will(self, *plans: dict) -> None:
        """What each run in turn prints and exits with; the last one repeats."""
        (self.home / "plans.json").write_text(json.dumps(plans))

    def replays(self, *recordings: dict) -> None:
        """Answer with recorded runs of the real CLI (tests/contract/fixtures/llm_cli)."""
        self.will(*({"stdout": "".join(json.dumps(line) + "\n" for line in r["stdout"]), "exit": r["exit"]}
                    for r in recordings))

    def seen(self, index: int = -1) -> dict:
        """How run `index` was started. Its scratch files are kept here: they are gone by now."""
        runs = len(list(self.home.glob("seen-*.json")))
        run = json.loads((self.home / f"seen-{index % runs}.json").read_text())
        run["files"] = {name: base64.b64decode(data) for name, data in run["files"].items()}
        return run
