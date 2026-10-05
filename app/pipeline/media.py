"""Media handling for a turn (spec 7.2 step 3). Until MediaStore lands, only voice notes are
fetched: they are transcribed in memory and just the transcript is kept."""
from typing import Any

import structlog

from app.channels.base import ChannelAdapter
from app.core.envelope import MediaRef
from app.llm.stt import SpeechToText

log = structlog.get_logger()


async def transcribe_audio(media: list[dict[str, Any]], adapter: ChannelAdapter | None,
                           stt: SpeechToText | None) -> bool:
    """Fill in `transcript` on untranscribed audio refs. True if anything changed."""
    changed = False
    for ref in media:
        if ref["kind"] != "audio" or ref.get("transcript") or adapter is None or stt is None:
            continue
        try:
            audio, mime = await adapter.fetch_media(MediaRef.model_validate(ref))
            ref["transcript"] = await stt.transcribe(audio, mime)
            changed = True
        except Exception as exc:
            log.warning("transcription_failed", error=type(exc).__name__)
    return changed
