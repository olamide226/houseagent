"""LLM adapters translate neutral types to each wire format and back (spec section 8.1 table).

The vendor SDKs speak httpx2, so the fake provider is a transport, not a respx route."""
import json

import httpx2
import pytest

from app.llm.anthropic import AnthropicClient
from app.llm.openai_compat import OpenAICompatClient
from app.llm.types import CACHE_BREAK, ChatMessage, ImagePart, LLMError, TextPart, ToolCall, ToolDef

TOOLS = [ToolDef(name="log_inventory", description="Record stock", parameters={"type": "object", "properties": {}})]
SYSTEM = "static rules" + CACHE_BREAK + "Now: Monday"


class Provider:
    """Answers each request with the next canned reply and keeps what was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        status, body = self.replies.pop(0)
        return httpx2.Response(status, json=body)

    def client(self):
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self))

    def sent(self, index=-1):
        return json.loads(self.requests[index].content)


def conversation():
    return [
        ChatMessage(role="user", content=[TextPart(text="Ola: out of eggs"),
                                          ImagePart(mime="image/jpeg", data_b64="aGVsbG8=")]),
        ChatMessage(role="assistant", tool_calls=[ToolCall(id="c1", name="log_inventory", arguments={"a": 1}),
                                                  ToolCall(id="c2", name="log_inventory", arguments={"a": 2})]),
        ChatMessage(role="tool", tool_call_id="c1", content=[TextPart(text="OK: egg finished")]),
        ChatMessage(role="tool", tool_call_id="c2", content=[TextPart(text="ERROR: nope")], is_error=True),
    ]


def openai_reply(message, finish="stop", usage=None):
    return 200, {"id": "x", "object": "chat.completion", "created": 1, "model": "m",
                 "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", **message}}],
                 "usage": usage or {"prompt_tokens": 120, "completion_tokens": 7, "total_tokens": 127,
                                    "prompt_tokens_details": {"cached_tokens": 100}}}


def openai_client(provider):
    return OpenAICompatClient(api_key="k", model="some-model", base_url="http://llm.test/v1",
                              http_client=provider.client())


async def test_openai_request_translation():
    provider = Provider(openai_reply({"content": "ACK"}))
    await openai_client(provider).complete(SYSTEM, conversation(), TOOLS, max_tokens=256, temperature=0.1)
    sent = provider.sent()
    assert str(provider.requests[0].url) == "http://llm.test/v1/chat/completions"
    assert (sent["model"], sent["max_tokens"], sent["temperature"]) == ("some-model", 256, 0.1)
    assert sent["tools"] == [{"type": "function", "function": {
        "name": "log_inventory", "description": "Record stock", "parameters": {"type": "object", "properties": {}}}}]
    system, user, assistant, first, second = sent["messages"]
    assert system == {"role": "system", "content": "static rules\n\nNow: Monday"}
    assert user["content"] == [{"type": "text", "text": "Ola: out of eggs"},
                               {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,aGVsbG8="}}]
    assert assistant["tool_calls"][0] == {"id": "c1", "type": "function",
                                          "function": {"name": "log_inventory", "arguments": '{"a": 1}'}}
    assert first == {"role": "tool", "tool_call_id": "c1", "content": "OK: egg finished"}
    assert second == {"role": "tool", "tool_call_id": "c2", "content": "ERROR: nope"}


async def test_openai_response_translation():
    calls = [{"id": "c9", "type": "function", "function": {"name": "log_inventory", "arguments": '{"changes": []}'}}]
    provider = Provider(openai_reply({"content": None, "tool_calls": calls}, finish="tool_calls"),
                        openai_reply({"content": "Done."}),
                        openai_reply({"content": "cut"}, finish="length"))
    client = openai_client(provider)
    asked = await client.complete(SYSTEM, [], TOOLS)
    assert asked.stop == "tool_calls" and asked.text is None
    assert asked.tool_calls == [ToolCall(id="c9", name="log_inventory", arguments={"changes": []})]
    assert (asked.usage.input_tokens, asked.usage.output_tokens, asked.usage.cached_tokens) == (120, 7, 100)
    done = await client.complete(SYSTEM, [], TOOLS)
    assert (done.stop, done.text) == ("end", "Done.")
    assert (await client.complete(SYSTEM, [], TOOLS)).stop == "length"


async def test_openai_malformed_tool_arguments_become_a_tool_error():
    calls = [{"id": "c9", "type": "function", "function": {"name": "log_inventory", "arguments": '{"changes": ['}}]
    provider = Provider(openai_reply({"content": None, "tool_calls": calls}, finish="tool_calls"))
    (call,) = (await openai_client(provider).complete(SYSTEM, [], TOOLS)).tool_calls
    assert (call.arguments, call.error) == ({}, "invalid JSON arguments")


async def test_openai_retries_rate_limits_then_raises_a_neutral_error(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    provider = Provider((429, {"error": {"message": "slow down"}}), openai_reply({"content": "ok"}))
    assert (await openai_client(provider).complete(SYSTEM, [], [])).text == "ok"
    assert len(provider.requests) == 2

    refused = Provider((401, {"error": {"message": "bad key"}}))
    with pytest.raises(LLMError):
        await openai_client(refused).complete(SYSTEM, [], [])


def anthropic_reply(content, stop="end_turn", usage=None):
    return 200, {"id": "msg_1", "type": "message", "role": "assistant", "model": "m", "content": content,
                 "stop_reason": stop, "stop_sequence": None,
                 "usage": usage or {"input_tokens": 20, "output_tokens": 7, "cache_read_input_tokens": 100,
                                    "cache_creation_input_tokens": 30}}


def anthropic_client(provider):
    return AnthropicClient(api_key="k", model="some-model", base_url="http://llm.test", http_client=provider.client())


async def test_anthropic_request_translation():
    provider = Provider(anthropic_reply([{"type": "text", "text": "ACK"}]))
    history = [ChatMessage(role="assistant", content=[TextPart(text="stale greeting")]), *conversation()]
    await anthropic_client(provider).complete(SYSTEM, history, TOOLS, max_tokens=256)
    sent = provider.sent()
    assert str(provider.requests[0].url) == "http://llm.test/v1/messages"
    assert (sent["model"], sent["max_tokens"]) == ("some-model", 256)
    assert "temperature" not in sent
    assert sent["system"] == [{"type": "text", "text": "static rules", "cache_control": {"type": "ephemeral"}},
                              {"type": "text", "text": "Now: Monday"}]
    assert sent["tools"] == [{"name": "log_inventory", "description": "Record stock",
                              "input_schema": {"type": "object", "properties": {}}}]
    user, assistant, results = sent["messages"]          # the leading assistant turn is dropped
    assert user == {"role": "user", "content": [
        {"type": "text", "text": "Ola: out of eggs"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "aGVsbG8="}}]}
    assert assistant == {"role": "assistant", "content": [
        {"type": "tool_use", "id": "c1", "name": "log_inventory", "input": {"a": 1}},
        {"type": "tool_use", "id": "c2", "name": "log_inventory", "input": {"a": 2}}]}
    assert results == {"role": "user", "content": [                 # both results in one user turn
        {"type": "tool_result", "tool_use_id": "c1", "is_error": False,
         "content": [{"type": "text", "text": "OK: egg finished"}]},
        {"type": "tool_result", "tool_use_id": "c2", "is_error": True,
         "content": [{"type": "text", "text": "ERROR: nope"}]}]}


async def test_anthropic_response_translation_and_reasoning_round_trip():
    thinking = {"type": "thinking", "thinking": "eggs are out", "signature": "sig"}
    tool_use = {"type": "tool_use", "id": "t1", "name": "log_inventory", "input": {"changes": []}}
    provider = Provider(anthropic_reply([thinking, tool_use], stop="tool_use"),
                        anthropic_reply([{"type": "text", "text": "Done."}]),
                        anthropic_reply([{"type": "text", "text": "cut"}], stop="max_tokens"),
                        anthropic_reply([], stop="refusal"))
    client = anthropic_client(provider)
    asked = await client.complete(SYSTEM, [ChatMessage(role="user", content=[TextPart(text="hi")])], TOOLS)
    assert asked.stop == "tool_calls"
    assert asked.tool_calls == [ToolCall(id="t1", name="log_inventory", arguments={"changes": []})]
    assert (asked.usage.input_tokens, asked.usage.output_tokens, asked.usage.cached_tokens) == (150, 7, 100)

    # The loop hands the opaque blocks back; the provider must receive its reasoning block unchanged.
    follow_up = [
        ChatMessage(role="user", content=[TextPart(text="hi")]),
        ChatMessage(role="assistant", tool_calls=asked.tool_calls, opaque=asked.opaque),
        ChatMessage(role="tool", tool_call_id="t1", content=[TextPart(text="OK")]),
    ]
    done = await client.complete(SYSTEM, follow_up, TOOLS)
    assert (done.stop, done.text) == ("end", "Done.")
    echoed = provider.sent()["messages"][1]["content"]
    assert [block["type"] for block in echoed] == ["thinking", "tool_use"]
    assert (echoed[0]["thinking"], echoed[0]["signature"]) == ("eggs are out", "sig")

    assert (await client.complete(SYSTEM, follow_up, TOOLS)).stop == "length"
    assert (await client.complete(SYSTEM, follow_up, TOOLS)).stop == "error"


async def test_anthropic_errors_become_neutral_errors():
    provider = Provider((400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}))
    with pytest.raises(LLMError):
        await anthropic_client(provider).complete(SYSTEM, [ChatMessage(role="user", content=[TextPart(text="x")])], [])


async def _no_sleep(_seconds):
    return None
