"""Speech-to-text adapter: the OpenAI-compatible transcription call, mocked with respx."""
import re

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from app.config import Settings
from app.llm.stt import OpenAICompatSTT, make_stt
from app.pipeline import media as media_pipeline
from tests.helpers import FakeAdapter

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


# What each channel hands over: Telegram and WhatsApp voice notes, WhatsApp audio, the MP3 or M4A
# BlueBubbles gives for iMessage, and the WAV the pipeline makes of an iMessage CAF.
@pytest.mark.parametrize("mime, name", [
    ("audio/ogg", "audio.ogg"), ("audio/ogg; codecs=opus", "audio.ogg"), ("audio/mpeg", "audio.mp3"),
    ("audio/mp4", "audio.m4a"), ("audio/x-m4a", "audio.m4a"), ("audio/wav", "audio.wav"), ("audio/webm", "audio.webm"),
])
@respx.mock
async def test_the_upload_is_named_with_the_extension_the_transcriber_reads_the_format_from(mime, name):
    """Groq answers 400 unsupported_audio_format to a file called `audio`, and 200 to the same bytes as `audio.ogg`."""
    route = respx.post("http://stt.test/v1/audio/transcriptions").respond(json={"text": "ok"})
    await OpenAICompatSTT(base_url="http://stt.test/v1", api_key="k", model="m").transcribe(b"bytes", mime)
    (disposition,) = re.findall(rb'Content-Disposition: form-data; name="file"; filename="([^"]*)"',
                                route.calls.last.request.content)
    assert disposition.decode() == name


@respx.mock
async def test_a_refused_voice_note_is_logged_with_the_status_and_the_reason_but_never_the_key():
    respx.post("http://stt.test/v1/audio/transcriptions").respond(400, json={"error": {
        "message": "file must be one of the following types: [flac mp3]", "code": "unsupported_audio_format"}})
    stt = OpenAICompatSTT(base_url="http://stt.test/v1", api_key="stt-secret-key", model="m")
    media = [{"kind": "audio", "mime": "audio/ogg", "external_id": "V1"}]
    with capture_logs() as logs:
        assert not await media_pipeline.prepare(media, "house-1", "msg-1", FakeAdapter(), stt, None)
    assert logs == [{"event": "media_failed", "log_level": "warning", "household_id": "house-1", "message_id": "msg-1",
                     "kind": "audio", "step": "transcribe", "error": "HTTPStatusError", "status": 400,
                     "reason": "unsupported_audio_format"}]
    assert "transcript" not in media[0] and "stt-secret-key" not in str(logs)


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
