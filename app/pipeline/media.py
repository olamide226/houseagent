"""Media handling for a turn (spec 7.2 step 3): fetch each attachment once, keep it through
MediaStore, and transcribe voice notes. Images stay as references for the agent turn."""
import asyncio
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx
import structlog

from app.channels.base import ChannelAdapter
from app.core.envelope import MediaRef
from app.llm.stt import SpeechToText
from app.media.store import MediaStore

log = structlog.get_logger()
STORAGE_FIELDS = ("storage_backend", "storage_key", "storage_url", "delete_url")
CAF = "audio/x-caf"   # how an iMessage voice note arrives; transcribers do not take it


def caf_to_wav(data: bytes) -> bytes:
    """A voice note in Apple's CAF container as 16 kHz WAV: `ffmpeg -i in.caf -ar 16000 out.wav`."""
    with tempfile.TemporaryDirectory() as folder:
        source, target = Path(folder, "in.caf"), Path(folder, "out.wav")
        source.write_bytes(data)
        subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(source), "-ar", "16000", str(target)],
                       check=True, stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        return target.read_bytes()


def is_stored(ref: dict[str, Any]) -> bool:
    return bool(ref.get("storage_key"))


async def prepare(media: list[dict[str, Any]], household_id: str, message_id: str,
                  adapter: ChannelAdapter | None, stt: SpeechToText | None, store: MediaStore | None) -> bool:
    """Store unstored attachments and transcribe untranscribed audio. True if any ref changed.

    A failure costs only that attachment: the turn goes on and the agent is told what it lacks."""
    changed = False
    for n, ref in enumerate(media):
        wants_store = store is not None and not is_stored(ref)
        wants_transcript = ref["kind"] == "audio" and stt is not None and not ref.get("transcript")
        if adapter is None or not ref.get("external_id") or not (wants_store or wants_transcript):
            continue
        step = "fetch"
        try:
            data, mime = await adapter.fetch_media(MediaRef.model_validate(ref))
            if mime == CAF:
                step = "convert"
                data, mime = await asyncio.to_thread(caf_to_wav, data), "audio/wav"
            if store is not None and wants_store:
                step = "store"
                stored = await store.put(household_id, message_id, n, data, mime)
                ref.update(stored.model_dump(include=set(STORAGE_FIELDS), exclude_none=True))
                changed = changed or is_stored(ref)
            if stt is not None and wants_transcript:
                step = "transcribe"
                ref["transcript"] = await stt.transcribe(data, mime)
                changed = True
        except Exception as exc:
            log.warning("media_failed", household_id=household_id, message_id=message_id, kind=ref["kind"],
                        step=step, error=type(exc).__name__, **_refusal(exc))
    return changed


def _refusal(exc: Exception) -> dict[str, Any]:
    """What a service answered when it refused: the status and its short error code. Never the
    request, whose address or headers carry a key, nor the message, which may quote part of one."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return {}
    try:
        reason = exc.response.json()["error"]["code"]
    except (ValueError, KeyError, TypeError):
        reason = None
    return {"status": exc.response.status_code, **({"reason": str(reason)[:60]} if reason else {})}


def without_storage(media: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The refs once their stored copy is gone; captions and transcripts remain."""
    return [{k: v for k, v in ref.items() if k not in STORAGE_FIELDS} for ref in media]
