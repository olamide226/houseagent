"""A pass-through in front of DeepSeek's OpenAI-compatible endpoint for the Letta comparison.

Letta 0.16.8 shortens every tool-call id to 29 characters. DeepSeek's thinking mode keeps the
reasoning that came with a tool call on its side, keyed by the full id, and refuses the next
request ("The `reasoning_content` in the thinking mode must be passed back to the API") when the
id it is handed back is not one it issued. With a photo in the turn it wants the reasoning itself
back even under the right id. This restores the ids and hands each tool call's reasoning back with
it, and changes nothing else.

    DEEPSEEK_OPENAI_BASE_URL=https://api.deepseek.com uv run python tests/evals/letta_deepseek_shim.py [usage.jsonl]

Then start Letta with OPENAI_BASE_URL=http://host.docker.internal:9911/v1 and the DeepSeek key as
OPENAI_API_KEY. The key passes through in the Authorization header and is never logged.
"""
import json
import os
import sys

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response

UPSTREAM = os.environ["DEEPSEEK_OPENAI_BASE_URL"].rstrip("/")
USAGE_LOG = sys.argv[1] if len(sys.argv) > 1 else None
SHORT = 29
full_ids: dict[str, str] = {}
reasoning: dict[str, str] = {}   # by full tool-call id
app = FastAPI()


def _log_usage(usage: dict) -> None:
    with open(USAGE_LOG or "", "a") as log:
        log.write(json.dumps(usage) + "\n")


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def forward(path: str, request: Request) -> Response:
    body = await request.body()
    if body:
        payload = json.loads(body)
        for message in payload.get("messages", []):
            for call in message.get("tool_calls") or []:
                call["id"] = full_ids.get(call["id"], call["id"])
                if call["id"] in reasoning:
                    message["reasoning_content"] = reasoning[call["id"]]
            if message.get("tool_call_id"):
                message["tool_call_id"] = full_ids.get(message["tool_call_id"], message["tool_call_id"])
        body = json.dumps(payload).encode()
    headers = {"Authorization": request.headers.get("authorization", ""), "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=300) as client:
        upstream = await client.request(request.method, f"{UPSTREAM}/{path}", content=body or None, headers=headers)
    try:
        answer = upstream.json()
        for choice in answer.get("choices", []):
            for call in choice["message"].get("tool_calls") or []:
                full_ids[call["id"][:SHORT]] = call["id"]
                if choice["message"].get("reasoning_content"):
                    reasoning[call["id"]] = choice["message"]["reasoning_content"]
        if USAGE_LOG and "usage" in answer:
            _log_usage(answer["usage"])
    except (ValueError, KeyError, AttributeError):
        pass
    return Response(content=upstream.content, status_code=upstream.status_code, media_type="application/json")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9911, log_level="warning")
