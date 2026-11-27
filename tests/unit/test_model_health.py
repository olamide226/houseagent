"""When the model stops answering: each provider's failures sorted into the admin's to fix and the
ones that pass, the admins told once (and again once a day), the family told why, the admins told
when it answers again, and the System page, with nothing secret anywhere (ADR 0043)."""
import asyncio
import re
from datetime import timedelta

import httpx2
import pytest

from app.agent.loop import LoopRuntime
from app.core.envelope import Channel
from app.core.timeutil import utcnow
from app.db import execute, fetch_all, tx
from app.llm.anthropic import AnthropicClient
from app.llm.openai_compat import OpenAICompatClient
from app.llm.types import CAUSE_KIND, ChatMessage, LLMError, TextPart
from app.pipeline import inbound, router
from app.services import model_health
from app.services.model_health import ADMIN_REPLY, BACK_TEXT, CAUSES, DOWN_TEXT, TOLD_REPLY
from tests.helpers import FakeAdapter, add_member, london, say, seed_home, technical_words, tg_update
from tests.unit.test_dashboard import login
from tests.unit.test_llm_adapters import Provider

SECRET = {"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"}
HI = [ChatMessage(role="user", content=[TextPart(text="hi")])]
# Made up, in the shape OpenAI masks a refused key in its message.
MASKED_KEY = "sk-proj-abcd" + "*" * 20 + "wxyz"
NOT_A_KEY = "not-a-real-key-for-this-test"


async def _no_sleep(_seconds):
    return None


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)   # the SDKs back off between their own retries


async def anthropic_error(status, body, key="k"):
    provider = Provider(*[(status, body)] * 4)
    client = AnthropicClient(api_key=key, model="m", base_url="http://llm.test", http_client=provider.client())
    with pytest.raises(LLMError) as raised:
        await client.complete("s", HI, [])
    return raised.value


async def openai_error(status, body):
    provider = Provider(*[(status, body)] * 4)
    client = OpenAICompatClient(api_key="k", model="m", base_url="http://llm.test/v1", http_client=provider.client())
    with pytest.raises(LLMError) as raised:
        await client.complete("s", HI, [])
    return raised.value


def anthropic_body(error_type, message):
    return {"type": "error", "error": {"type": error_type, "message": message}, "request_id": "req_011CXtest"}


# ---------------------------------------------------------------- what each provider's failure means
@pytest.mark.parametrize("status,body,cause,error_type", [
    # What the live server got on 23 Nov 2026.
    (401, anthropic_body("authentication_error", "invalid x-api-key"), "key_rejected", "authentication_error"),
    (403, anthropic_body("permission_error", "Your API key does not have permission to use the specified resource."),
     "not_allowed", "permission_error"),
    (402, anthropic_body("billing_error", "Your account has a billing issue."), "out_of_credit", "billing_error"),
    (400, anthropic_body("invalid_request_error", "Your credit balance is too low to access the Anthropic API. "
                         "Please go to Plans & Billing to upgrade or purchase credits."), "out_of_credit",
     "invalid_request_error"),
    (404, anthropic_body("not_found_error", "model: claude-nonexistent"), "model_unknown", "not_found_error"),
    (429, anthropic_body("rate_limit_error", "Number of request tokens has exceeded your per-minute rate limit"),
     "busy", "rate_limit_error"),
    (529, anthropic_body("overloaded_error", "Overloaded"), "busy", "overloaded_error"),
    (500, anthropic_body("api_error", "Internal server error"), "unreachable", "api_error"),
    (400, anthropic_body("invalid_request_error", "messages: roles must alternate"), None, "invalid_request_error"),
])
async def test_anthropic_failures_are_sorted_by_what_the_admin_can_do(status, body, cause, error_type):
    error = await anthropic_error(status, body)
    assert (error.cause, error.error_type) == (cause, error_type)
    assert error.kind == (CAUSE_KIND.get(cause) if cause else None)


async def test_anthropic_with_no_key_is_a_missing_key_and_a_network_failure_is_one_that_passes():
    error = await anthropic_error(200, {}, key="")
    assert (error.cause, error.kind) == ("key_missing", "config")

    def unreachable(request):
        raise httpx2.ConnectError("connection refused", request=request)

    client = AnthropicClient(api_key="k", model="m", base_url="http://llm.test",
                             http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(unreachable)))
    with pytest.raises(LLMError) as raised:
        await client.complete("s", HI, [])
    assert (raised.value.cause, raised.value.kind, raised.value.error_type) == (
        "unreachable", "transient", "APIConnectionError")


@pytest.mark.parametrize("status,body,cause,error_type", [
    (401, {"error": {"message": f"Incorrect API key provided: {MASKED_KEY}. You can find your API key at "
                                "https://platform.openai.com/account/api-keys.", "type": "invalid_request_error",
                     "param": None, "code": "invalid_api_key"}}, "key_rejected", "invalid_api_key"),
    # DeepSeek's answers to a refused key and to an empty account.
    (401, {"error": {"message": "Authentication Fails, Your api key: ****wxyz is invalid",
                     "type": "authentication_error", "param": None, "code": "invalid_request_error"}},
     "key_rejected", "invalid_request_error"),
    (402, {"error": {"message": "Insufficient Balance", "type": "unknown_error", "param": None,
                     "code": "invalid_request_error"}}, "out_of_credit", "invalid_request_error"),
    (429, {"error": {"message": "You exceeded your current quota, please check your plan and billing details.",
                     "type": "insufficient_quota", "param": None, "code": "insufficient_quota"}},
     "out_of_credit", "insufficient_quota"),
    (429, {"error": {"message": "Rate limit reached for requests", "type": "requests", "param": None,
                     "code": "rate_limit_exceeded"}}, "busy", "rate_limit_exceeded"),
    (404, {"error": {"message": "The model `gpt-nope` does not exist or you do not have access to it.",
                     "type": "invalid_request_error", "param": None, "code": "model_not_found"}},
     "model_unknown", "model_not_found"),
    (403, {"error": {"message": "Project does not have access to model", "type": "invalid_request_error",
                     "param": None, "code": "model_not_found"}}, "not_allowed", "model_not_found"),
    (503, {"error": {"message": "Service Unavailable", "type": "server_error", "param": None, "code": None}},
     "unreachable", "server_error"),
    (400, {"error": {"message": "Invalid 'messages'", "type": "invalid_request_error", "param": None, "code": None}},
     None, "invalid_request_error"),
])
async def test_openai_compatible_failures_are_sorted_by_what_the_admin_can_do(status, body, cause, error_type):
    error = await openai_error(status, body)
    assert (error.cause, error.error_type) == (cause, error_type)
    assert MASKED_KEY not in str(error) and "****wxyz" not in str(error)   # the key, even masked, is taken out


# The subscription CLIs' recorded failures (tests/unit/test_llm_cli.py) are sorted the same way.
@pytest.mark.parametrize("detail,cause", [
    ("Not logged in · Please run /login", "signed_out"),
    ("unexpected status 401 Unauthorized: Missing bearer or basic authentication in header", "signed_out"),
    ("Your access token could not be refreshed because your refresh token was revoked.", "signed_out"),
    ("You've hit your usage limit. Try again at 3:15 PM.", "usage_limit"),
    ("The 'x' model is not supported when using Codex with a ChatGPT account.", "model_unknown"),
    ("There's an issue with the selected model (claude-x). It may not exist or you may not have access to it.",
     "model_unknown"),
    ("error: unknown option '--restricted'", None),
])
def test_subscription_cli_failures_are_sorted_too(detail, cause):
    from app.llm.cli import failure

    assert failure("Codex", detail).cause == cause


async def test_a_missing_cli_is_the_admins_and_a_cli_that_takes_too_long_passes(tmp_path):
    from app.llm.cli import run_cli

    with pytest.raises(LLMError) as raised:
        await run_cli("Codex", [str(tmp_path / "nowhere")], stdin="", cwd=str(tmp_path), seconds=5)
    assert raised.value.kind == "config" and raised.value.cause == "cli_missing"
    with pytest.raises(LLMError) as raised:
        await run_cli("Codex", ["sleep", "5"], stdin="", cwd=str(tmp_path), seconds=0.2)
    assert raised.value.kind == "transient" and raised.value.cause == "unreachable"


# ---------------------------------------------------------------- the pipeline
class Refusing:
    """A model whose every call fails as `error`, until it is given `answers`."""
    supports_images = True

    def __init__(self, error):
        self.error, self.answers = error, []

    async def complete(self, *args, **kwargs):
        if self.answers:
            return self.answers.pop(0)
        raise self.error


REFUSED = LLMError("AuthenticationError: Error code: 401 - invalid x-api-key", cause="key_rejected",
                   error_type="authentication_error")


async def rows(sql, **params):
    async with tx() as conn:
        return await fetch_all(conn, sql, **params)


async def family(conn):
    home = await seed_home(conn)
    await add_member(conn, home, "Ada", telegram_id="1002")
    return home


async def message(client, n, text, user_id=1001, name="Ola", **kwargs):
    assert (await client.post("/webhooks/telegram", json=tg_update(n, text, user_id=user_id, name=name, **kwargs),
                              headers=SECRET)).status_code == 200


async def outbox(prefix="%"):
    return await rows("select member_id, thread_id, text, dedupe_key, respect_quiet_hours, urgency from outbox "
                      "where coalesce(dedupe_key, '') like :p order by created_at, id", p=prefix)


async def test_a_refused_key_tells_the_admin_once_and_the_family_why(client):
    async with tx() as conn:
        home = await family(conn)
    llm = Refusing(REFUSED)
    runtime = LoopRuntime(llm)

    await message(client, 1, "out of eggs")
    await inbound.process_household(home.id, runtime, {})
    (alert,) = await outbox("model_down:%")
    assert alert["member_id"] == home.ola and alert["text"] == DOWN_TEXT.format(cause=CAUSES["key_rejected"])
    assert alert["respect_quiet_hours"] and alert["urgency"] == "normal"
    assert "can't reply to anyone right now: the AI service I use has stopped accepting my key" in alert["text"]
    replies = await rows("select text from outbox where dedupe_key is null")
    assert [r["text"] for r in replies] == [ADMIN_REPLY.format(cause=CAUSES["key_rejected"])]

    # Ada, in her own chat, and then both in the family group: told plainly, and no second alert.
    await message(client, 2, "is the bin tonight?", user_id=1002, name="Ada")
    await inbound.process_household(home.id, runtime, {})
    await message(client, 3, "hello?", user_id=1002, name="Ada", chat_id=-100555, chat_type="supergroup")
    await message(client, 4, "anyone?", chat_id=-100555, chat_type="supergroup")
    await inbound.process_household(home.id, runtime, {})
    replies = [r["text"] for r in await rows("select text from outbox where dedupe_key is null order by created_at")]
    assert replies[1:] == [TOLD_REPLY.format(admins="Ola")] * 2
    assert replies[1] == "Sorry, I can't answer right now: the AI service I use has stopped working. I've let Ola know."
    assert len(await outbox("model_down:%")) == 1
    (outage,) = await rows("select kind, cause, error_type, failures, ended_at from model_outages")
    assert outage == {"kind": "config", "cause": "key_rejected", "error_type": "authentication_error",
                      "failures": 3, "ended_at": None}
    for text in [alert["text"], *replies]:
        assert technical_words(text) == [], text

    # The key is replaced and the next message is answered: one "answering again", to whoever heard.
    await execute_sql("update outbox set status = 'sent' where dedupe_key like 'model_down:%'")
    llm.answers = [say("Yes, bins tonight.")]
    await message(client, 5, "bins tonight?", user_id=1002, name="Ada")
    await inbound.process_household(home.id, runtime, {})
    (back,) = await outbox("model_back:%")
    assert (back["member_id"], back["text"]) == (home.ola, BACK_TEXT)
    assert (await rows("select ended_at is not null as ended from model_outages")) == [{"ended": True}]
    llm.answers = [say("Noted.")]
    await message(client, 6, "thanks")
    await inbound.process_household(home.id, runtime, {})
    assert len(await outbox("model_back:%")) == 1


async def execute_sql(sql, **params):
    async with tx() as conn:
        await execute(conn, sql, **params)


async def test_every_admin_of_every_household_is_told_and_two_replicas_tell_each_once():
    async with tx() as conn:
        home = await family(conn)
        await execute(conn, "update members set is_admin = true where id = :m", m=home.members["Ada"])
        other = await seed_home(conn, telegram_id="2001")
    now = utcnow()

    async def fail():
        async with tx() as conn:
            return await model_health.failed(conn, REFUSED, "anthropic", now)

    await asyncio.gather(*(fail() for _ in range(4)))
    await fail()                                                   # a restart: the same open outage
    alerts = await outbox("model_down:%")
    assert sorted(a["member_id"] for a in alerts) == sorted([home.ola, home.members["Ada"], other.ola])
    assert (await rows("select failures from model_outages")) == [{"failures": 5}]


async def test_a_lasting_outage_is_said_again_once_a_day_on_a_turn_that_fails():
    async with tx() as conn:
        home = await seed_home(conn)
    start = london("2026-11-23 09:00")

    async def fail(at):
        async with tx() as conn:
            await model_health.failed(conn, REFUSED, "anthropic", at)

    await fail(start)
    await fail(start + timedelta(hours=23))
    assert len(await outbox("model_down:%")) == 1
    await fail(start + timedelta(hours=24, minutes=5))
    await fail(start + timedelta(hours=30))
    alerts = await outbox("model_down:%")
    assert len(alerts) == 2 and alerts[1]["member_id"] == home.ola
    assert alerts[1]["text"] == ("I still can't reply to anyone, since Monday 23 November at 09:00: "
                                 f"{CAUSES['key_rejected']}. {model_health.FIX}")
    await fail(start + timedelta(hours=48, minutes=6))
    assert len(await outbox("model_down:%")) == 3


async def test_a_passing_failure_tells_nobody_until_every_turn_has_failed_for_half_an_hour():
    async with tx() as conn:
        home = await seed_home(conn)
    busy = LLMError("OverloadedError: Overloaded", cause="busy", error_type="overloaded_error")
    start = utcnow()

    async def fail(at, error=busy):
        async with tx() as conn:
            return await model_health.failed(conn, error, "anthropic", at)

    async def works(at):
        async with tx() as conn:
            return await model_health.succeeded(conn, at)

    # A blip: a failure, then an answer. Nobody is told and no outage is kept.
    await fail(start)
    assert await works(start + timedelta(minutes=1)) == 0
    assert await rows("select 1 from model_outages") == []

    outage = await fail(start + timedelta(minutes=2))
    assert await model_health_reply(home, outage) is None          # the usual apology meanwhile
    await fail(start + timedelta(minutes=20))
    await fail(start + timedelta(minutes=31))
    assert await outbox("model_down:%") == []                      # 29 minutes since the first failure
    outage = await fail(start + timedelta(minutes=33))
    (alert,) = await outbox("model_down:%")
    assert alert["text"].startswith(f"I haven't been able to reply to anyone for the last 31 minutes: {CAUSES['busy']}.")
    assert await model_health_reply(home, outage) == ADMIN_REPLY.format(cause=CAUSES["busy"])
    # An error nobody recognised counts as passing too.
    assert model_health.cause_of(RuntimeError("x")) == ("transient", "failed", None)


async def model_health_reply(home, outage, member=None, scope="dm"):
    async with tx() as conn:
        return await model_health.reply_for(conn, outage, home.id, member or home.ola, scope)


async def test_a_warning_still_held_for_quiet_hours_is_dropped_if_it_answers_again_before_morning():
    async with tx() as conn:
        home = await seed_home(conn)
        await execute(conn, "update members set quiet_start = '21:30', quiet_end = '07:00' where id = :m", m=home.ola)
    night = london("2026-11-24 23:00")
    async with tx() as conn:
        await model_health.failed(conn, REFUSED, "anthropic", night)
    adapter = FakeAdapter()
    await router.dispatch_due({Channel.telegram: adapter}, now=night)
    assert adapter.sent == []
    assert (await rows("select status, send_after from outbox")) == [
        {"status": "pending", "send_after": london("2026-11-25 07:00")}]

    async with tx() as conn:
        assert await model_health.succeeded(conn, night + timedelta(hours=1)) == 0   # nobody heard it was down
    await router.dispatch_due({Channel.telegram: adapter}, now=london("2026-11-25 07:00"))
    assert (await rows("select status from outbox")) == [{"status": "cancelled"}]
    assert adapter.sent == []


async def test_the_alert_goes_out_without_the_model(client):
    """The warning is fixed words in the outbox: dispatching it calls no model at all."""
    async with tx() as conn:
        home = await seed_home(conn)
    await message(client, 1, "hi")
    await inbound.process_household(home.id, LoopRuntime(Refusing(REFUSED)), {})
    adapter = FakeAdapter()
    await router.dispatch_due({Channel.telegram: adapter})
    assert DOWN_TEXT.format(cause=CAUSES["key_rejected"]) in [text for _, text, _ in adapter.sent]


async def test_nothing_secret_reaches_the_messages_the_outbox_the_logs_or_the_system_page(client, capsys):
    async with tx() as conn:
        home = await seed_home(conn)
    provider = Provider(*[(401, {"error": {"message": f"Incorrect API key provided: {MASKED_KEY}.",
                                           "type": "invalid_request_error", "code": "invalid_api_key"}})] * 4)
    llm = OpenAICompatClient(api_key=NOT_A_KEY, model="m", base_url="http://llm.test/v1",
                             http_client=provider.client())
    await message(client, 1, "hi")
    await inbound.process_household(home.id, LoopRuntime(llm), {})

    (stored,) = await rows("select meta->>'error' as error from messages")
    assert stored["error"].startswith("LLMError: AuthenticationError: Error code: 401")
    await login(client, home)
    page = re.sub(r"\s+", " ", (await client.get("/dashboard/system")).text)
    assert "Not answering anyone since " in page and CAUSES["key_rejected"] in page
    assert "<span>invalid_api_key <span class=\"muted\">openai_compat</span></span>" in page
    logged = capsys.readouterr()
    for text in [stored["error"], page, logged.out, logged.err,
                 *(r["text"] for r in await rows("select text from outbox"))]:
        assert "abcd****" not in text and MASKED_KEY not in text and NOT_A_KEY not in text
    assert "Traceback" not in logged.out + logged.err


async def test_the_system_page_shows_the_last_answer_and_earlier_outages(client):
    async with tx() as conn:
        home = await seed_home(conn)
    await login(client, home)
    page = re.sub(r"\s+", " ", (await client.get("/dashboard/system")).text)
    assert "No message has been answered here yet." in page

    await message(client, 1, "hi")
    llm = Refusing(REFUSED)
    await inbound.process_household(home.id, LoopRuntime(llm), {})
    llm.answers = [say("Hello!")]
    await message(client, 2, "hi again")
    await inbound.process_household(home.id, LoopRuntime(llm), {})
    page = re.sub(r"\s+", " ", (await client.get("/dashboard/system")).text)
    assert "<span class=\"dot good\"></span>Answering. Last answered a message here " in page
    assert "Earlier outages" in page and f"{CAUSES['key_rejected']} (authentication_error)" in page
    assert "1 unanswered" in page
