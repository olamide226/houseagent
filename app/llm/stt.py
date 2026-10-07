"""Speech-to-text behind one protocol; voice notes become transcripts before the agent turn."""
from typing import Protocol

import httpx

from app.config import Settings
from app.media.store import EXTENSIONS


class SpeechToText(Protocol):
    async def transcribe(self, audio: bytes, mime: str) -> str: ...


def upload_name(mime: str) -> str:
    """The name the audio is uploaded under. A transcriber reads the format from its extension:
    Groq answers 400 unsupported_audio_format to a file called just `audio`."""
    kind = mime.split(";")[0].strip().lower()   # "audio/ogg; codecs=opus"
    return f"audio.{EXTENSIONS.get(kind) or kind.rpartition('/')[2].removeprefix('x-')}"


class OpenAICompatSTT:
    """Any endpoint exposing OpenAI's `POST /audio/transcriptions`."""

    def __init__(self, *, base_url: str, api_key: str, model: str) -> None:
        self._url = base_url.rstrip("/") + "/audio/transcriptions"
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._model = model

    async def transcribe(self, audio: bytes, mime: str) -> str:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                self._url, headers=self._headers,
                data={"model": self._model}, files={"file": (upload_name(mime), audio, mime)},
            )
        response.raise_for_status()
        return str(response.json()["text"]).strip()


def make_stt(settings: Settings) -> SpeechToText | None:
    if settings.stt_provider == "openai_compat" and settings.stt_base_url and settings.stt_model:
        return OpenAICompatSTT(
            base_url=settings.stt_base_url, api_key=settings.stt_api_key, model=settings.stt_model
        )
    return None
