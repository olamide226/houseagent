"""Channel contract suite: recorded webhook payloads parse into golden InboundEvent JSON.

Every adapter must pass this suite. A new channel adds a fixtures directory and a factory."""
import json
from pathlib import Path

import pytest

from app.channels.telegram import TelegramAdapter

FIXTURES = Path(__file__).parent / "fixtures"
REQUIRED_CASES = {"text", "voice", "photo", "location", "reaction", "group", "own_message"}
ADAPTERS = {
    "telegram": lambda: TelegramAdapter("424242:TEST-TOKEN", "test-webhook-secret"),
}
CASES = [(channel, case) for channel in ADAPTERS for case in sorted(REQUIRED_CASES)]


@pytest.mark.parametrize("channel", ADAPTERS)
def test_channel_has_every_required_fixture(channel):
    recorded = {path.stem for path in (FIXTURES / channel).glob("*.json")}
    golden = {path.stem for path in (FIXTURES / channel / "golden").glob("*.json")}
    assert recorded >= REQUIRED_CASES and golden >= REQUIRED_CASES


@pytest.mark.parametrize("channel,case", CASES, ids=[f"{channel}-{case}" for channel, case in CASES])
async def test_fixture_parses_into_golden_events(channel, case):
    body = (FIXTURES / channel / f"{case}.json").read_bytes()
    events = await ADAPTERS[channel]().parse(body)
    parsed = [e.model_dump(mode="json", exclude={"raw"}, exclude_defaults=True) for e in events]
    assert parsed == json.loads((FIXTURES / channel / "golden" / f"{case}.json").read_text())
    assert all(e.raw == json.loads(body) for e in events)


@pytest.mark.parametrize("channel", ADAPTERS)
async def test_own_messages_are_dropped(channel):
    assert await ADAPTERS[channel]().parse((FIXTURES / channel / "own_message.json").read_bytes()) == []
