"""Speech-to-text adapter: the OpenAI-compatible transcription call, mocked with respx."""
import httpx
import pytest
import respx

from app.config import Settings
from app.llm.stt import OpenAICompatSTT, make_stt

BASE = {"database_url": "x", "public_base_url": "x", "session_secret": "x", "llm_provider": "anthropic",
        "llm_model": "m"}


@respx.mock
async def test_transcribe_posts_the_audio_and_returns_the_text():
    route = respx.post("http://stt.test/v1/audio/transcriptions").respond(json={"text": " we need rice \n"})
    stt = OpenAICompatSTT(base_url="http://stt.test/v1/", api_key="stt-key", model="whisper-x")
    assert await stt.transcribe(b"OggS-bytes", "audio/ogg") == "we need rice"
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer stt-key"
    body = request.content
    assert b"OggS-bytes" in body and b"whisper-x" in body and b"audio/ogg" in body


@respx.mock
async def test_a_failed_transcription_raises():
    respx.post("http://stt.test/v1/audio/transcriptions").respond(500)
    with pytest.raises(httpx.HTTPStatusError):
        await OpenAICompatSTT(base_url="http://stt.test/v1", api_key="", model="m").transcribe(b"x", "audio/ogg")


def test_speech_to_text_is_optional():
    assert make_stt(Settings(**BASE, _env_file=None)) is None
    configured = Settings(**BASE, _env_file=None, stt_provider="openai_compat", stt_base_url="http://stt.test/v1",
                          stt_model="whisper-x")
    assert isinstance(make_stt(configured), OpenAICompatSTT)
