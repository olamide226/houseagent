"""LoopRuntime: the plain tool-calling loop (spec section 8.2)."""
import base64
from typing import Any

import structlog

from app.agent.base import AgentResult, Ctx, ToolCallRecord
from app.agent.prompt import build_brief, system_prompt
from app.agent.tools import run_tool, tool_definitions
from app.core.envelope import Envelope
from app.db import fetch_all
from app.llm.types import ChatMessage, ImagePart, LLMClient, LLMError, TextPart, Usage
from app.media.store import MediaStore
from app.services import households

log = structlog.get_logger()
HISTORY_MESSAGES = 20
HISTORY_HOURS = 48
MAX_TOKENS = 4096   # room for reasoning models; replies themselves are a line or two
LOST = "I got a bit lost there, can you say that again?"
NO_PHOTOS = "[photo received; this model can't read photos]"
UNREADABLE = "[photo received; it could not be loaded]"
MAX_IMAGES = 4


def message_lines(row: dict[str, Any], reaction_target: str | None = None) -> list[str]:
    """One inbound message as the text lines the agent reads (spec 7.2 step 4)."""
    lines: list[str] = []
    meta = row["meta"]
    if meta.get("reaction_emoji"):
        lines.append(f'[reacted {meta["reaction_emoji"]} to: "{(reaction_target or "")[:120]}"]')
    if row["text"]:
        lines.append(row["text"])
    for media in row["media"]:
        if media["kind"] == "audio":
            lines.append(f"[voice note] {media.get('transcript') or '(could not be transcribed)'}")
        elif media["kind"] == "image":
            lines.append(f"[photo] {media.get('caption') or ''}".rstrip())
        elif media["kind"] == "location":
            lines.append(f"[location] {media.get('lat')}, {media.get('lng')}")
        else:
            lines.append(f"[{media['kind']}]")
    return lines


def _text(role: Any, text: str) -> ChatMessage:
    return ChatMessage(role=role, content=[TextPart(text=text)])


class LoopRuntime:
    def __init__(self, llm: LLMClient, *, media: MediaStore | None = None, agent_name: str = "Home",
                 max_iterations: int = 8) -> None:
        self._llm = llm
        self._media = media
        self._agent_name = agent_name
        self._max_iterations = max_iterations

    async def handle(self, env: Envelope, ctx: Ctx) -> AgentResult:
        onboarding = await households.onboarding(ctx.conn, env.household_id)
        system = system_prompt(self._agent_name, await build_brief(ctx.conn, env, env.received_at), onboarding)
        messages = await self._history(env, ctx)
        turn = env.text
        if env.reply_to_text:
            turn = f'[replying to: "{env.reply_to_text[:120]}"]\n{turn}'
        photos, notes = await self._photos(env)
        messages.append(ChatMessage(role="user", content=[TextPart(text="\n".join([turn, *notes])), *photos]))
        tools = tool_definitions(onboarding_active=onboarding["step"] is not None)

        records: list[ToolCallRecord] = []
        usage = Usage()
        for _ in range(self._max_iterations):
            response = await self._llm.complete(system, messages, tools, max_tokens=MAX_TOKENS)
            usage += response.usage
            if not response.tool_calls:
                if response.stop == "error":
                    raise LLMError("the model stopped without an answer")
                return self._final(response.text, records, usage)
            messages.append(ChatMessage(
                role="assistant", content=[TextPart(text=response.text)] if response.text else [],
                tool_calls=response.tool_calls, opaque=response.opaque,
            ))
            for call in response.tool_calls:
                if call.error:
                    result, is_error = f"ERROR: {call.error}", True
                else:
                    result, is_error = await run_tool(call.name, call.arguments, ctx)
                records.append(ToolCallRecord(name=call.name, args=call.arguments, result=result, is_error=is_error))
                messages.append(ChatMessage(role="tool", content=[TextPart(text=result)],
                                            tool_call_id=call.id, is_error=is_error))
        return AgentResult(reply=LOST, tool_calls=records, usage=usage)

    async def _photos(self, env: Envelope) -> tuple[list[ImagePart], list[str]]:
        """Up to four of the turn's photos, loaded through MediaStore, and a note for each one
        the model will not see."""
        if not env.images:
            return [], []
        if not self._llm.supports_images:
            return [], [NO_PHOTOS]
        photos: list[ImagePart] = []
        notes: list[str] = []
        for ref in env.images[:MAX_IMAGES]:
            try:
                if self._media is None or not (ref.storage_key or ref.storage_url):
                    raise LookupError("not stored")
                data = await self._media.get(ref)
                photos.append(ImagePart(mime=ref.mime or "image/jpeg", data_b64=base64.b64encode(data).decode()))
            except Exception as exc:
                log.warning("photo_unreadable", household_id=env.household_id, error=type(exc).__name__)
                notes.append(UNREADABLE)
        extra = len(env.images) - MAX_IMAGES
        if extra > 0:
            notes.append(f"[{extra} more photo{'' if extra == 1 else 's'} not read: at most {MAX_IMAGES} per message]")
        return photos, notes

    @staticmethod
    def _final(text: str | None, records: list[ToolCallRecord], usage: Usage) -> AgentResult:
        answer = (text or "").strip()
        wrote = any(not record.is_error for record in records)
        if answer == "ACK" or (not answer and wrote):
            return AgentResult(reply=None, ack_only=True, tool_calls=records, usage=usage)
        if answer == "NOOP" or not answer:
            return AgentResult(reply=None, noop=True, tool_calls=records, usage=usage)
        return AgentResult(reply=answer, tool_calls=records, usage=usage)

    async def _history(self, env: Envelope, ctx: Ctx) -> list[ChatMessage]:
        """The thread's last 20 messages from the past 48 hours, before this turn."""
        if env.thread_id is None:
            return []
        rows = await fetch_all(
            ctx.conn,
            """select m.direction, m.text, m.media, m.meta, mem.name
               from messages m left join members mem on mem.id = m.member_id
               where m.thread_id = :thread and not m.id = any(cast(:current as uuid[]))
                 and m.created_at > cast(:before as timestamptz) - make_interval(hours => :hours)
               order by m.created_at desc, m.id limit :limit""",
            thread=env.thread_id, current=env.message_ids, before=env.received_at,
            hours=HISTORY_HOURS, limit=HISTORY_MESSAGES,
        )
        history: list[ChatMessage] = []
        for row in reversed(rows):
            if row["direction"] == "out":
                history.append(_text("assistant", row["text"] or "ACK"))   # a bare reaction was an ACK
            elif lines := message_lines(row):
                history.append(_text("user", f"{row['name'] or 'Someone'}: " + "\n".join(lines)))
        return history
