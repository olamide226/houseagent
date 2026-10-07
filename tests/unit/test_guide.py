"""The product guide says what is built: a chat word, a page, a tool or a scheduled message that
has no line in it fails here, and so does a line that has outlived its code (ADR 0034)."""
import re
from pathlib import Path

import app.dashboard
from app.agent import actions, loop
from app.agent.guide import PAGES, SENDS, TALK, WORDS, Setup, product_guide
from app.agent.prompt import STATIC_PROMPT, build_brief, system_prompt
from app.agent.runtime import make_runtime, setup_of
from app.agent.tools import REGISTRY
from app.agent.tools.calendar import ScheduleEvent
from app.agent.tools.undo import UndoLast
from app.config import get_settings
from app.core.identity import INVITE_TTL
from app.db import execute, tx
from app.llm.types import CACHE_BREAK
from app.pipeline import inbound
from app.services import members
from app.worker import jobs
from app.worker.main import SCHEDULED
from tests.helpers import add_member, london, seed_home, technical_words
from tests.unit.test_loop import envelope

EVERYTHING = Setup(chat_apps=("telegram", "whatsapp", "imessage"), photos=True, voice_notes=True, shop_shortcut=True)
SENDS_NOTHING = {"expand_recurrence"}   # makes the reminder rows that fire_reminders later sends
NOW = london("2026-10-05 12:00")


def menu() -> dict[str, str]:
    """The pages in the dashboard's menu: the address after /dashboard, and the name shown."""
    html = (Path(app.dashboard.__file__).parent / "templates" / "base.html").read_text()
    return dict(re.findall(r'\{\{ to\("([a-z]*)", "[a-z]+", "([^"]+)"\) \}\}', html))


def test_every_word_page_tool_and_scheduled_message_has_a_line_and_no_line_is_left_over():
    assert set(WORDS) == set(inbound.KEYWORDS)
    assert {page: name for page, (name, _) in PAGES.items()} == menu() and len(menu()) == 10
    assert {tool for tools in TALK for tool in tools} == {
        name for name, spec in REGISTRY.items() if not spec.onboarding_only}
    assert set(SENDS) == set(SCHEDULED) - SENDS_NOTHING
    guide = product_guide(Setup())
    for word in WORDS:
        assert f"The word {word}, sent to you" in guide
    for name, about in PAGES.values():
        assert f"{name} ({about})" in guide
    for line in [*TALK.values(), *SENDS.values()]:
        assert line in guide


def test_the_numbers_in_the_guide_are_the_codes():
    guide = product_guide(EVERYTHING)
    assert f"for {members.LOGIN_TTL.seconds // 60} minutes" in WORDS["dashboard"]
    assert f"for {INVITE_TTL.days} days" in guide
    most = UndoLast.model_fields["n"].metadata[1].le
    assert f"(up to {most}, from the last {actions.UNDO_WINDOW_HOURS} hours)" in TALK[("undo_last",)]
    assert f"Up to {loop.MAX_IMAGES} photos" in guide
    assert ScheduleEvent.model_fields["remind_before_minutes"].default == [24 * 60, 60]
    assert "the day before and an hour before" in SENDS["fire_reminders"]
    assert f"Sundays at {jobs.WEEKLY_DIGEST_AT:%H:%M}" in SENDS["weekly_digest"]
    assert f"at {jobs.LOW_STOCK_PROMPT_AT:%H:%M}" in SENDS["low_stock_prompt"]


def test_the_guide_is_in_family_words_whatever_is_switched_on():
    for setup in (Setup(), EVERYTHING, Setup(photos=False)):
        guide = product_guide(setup)
        assert technical_words(guide) == []


def test_the_guide_only_says_what_this_installation_has_switched_on():
    bare, full = product_guide(Setup(photos=False)), product_guide(EVERYTHING)
    assert "cannot read photos in this household yet" in bare and "receipt" not in bare
    assert "a photo of a receipt records what was bought" in full and "cannot read photos" not in full
    assert "cannot listen to voice notes in this household yet" in bare
    assert "Voice notes work like typed messages." in full
    assert "this household can use Telegram. WhatsApp and iMessage are not set up here: adding one" in bare
    assert "New invite" not in bare                    # there is no other app to move to
    assert "this household can use Telegram, WhatsApp and iMessage. To be messaged in another" in full
    assert "whoever set this up has one thing to do first" in bare
    assert "one thing to do first" not in full


def test_what_is_switched_on_is_read_from_the_settings_and_the_media_store():
    settings = get_settings()        # as the tests configure it: three chat apps, no transcriber, no shortcut
    assert setup_of(settings, None) == Setup(chat_apps=("telegram", "whatsapp", "imessage"), photos=False)
    stored = object()
    assert setup_of(settings, stored).photos                                                    # type: ignore[arg-type]
    assert not setup_of(settings.model_copy(update={"llm_supports_images": False}), stored).photos   # type: ignore[arg-type]
    other = settings.model_copy(update={
        "wa_app_secret": None, "bb_password": None, "stt_provider": "openai_compat",
        "stt_base_url": "http://stt.test/v1", "stt_model": "whisper", "presence_shortcut_url": "https://icloud.test/s"})
    assert setup_of(other, None) == Setup(chat_apps=("telegram",), photos=False, voice_notes=True, shop_shortcut=True)
    assert make_runtime(other, None)._setup == setup_of(other, None)   # and it reaches the runtime that answers


def test_the_guide_follows_the_static_prompt_unchanged_and_stays_in_the_cached_part():
    static, brief = system_prompt("Hearth", "Now: Monday", setup=EVERYTHING).split(CACHE_BREAK)
    assert static == STATIC_PROMPT.replace("{AGENT_NAME}", "Hearth") + "\n" + product_guide(EVERYTHING)
    assert brief == "Now: Monday"
    assert system_prompt("Hearth", "Now: Monday").split(CACHE_BREAK)[0].endswith(product_guide(Setup()))


async def test_the_brief_says_what_the_answer_depends_on_for_this_person_and_household():
    async with tx() as conn:
        home = await seed_home(conn)
        ada = await add_member(conn, home, "Ada", telegram_id="1002", whatsapp_id="447700900002")
        await add_member(conn, home, "Grace")
        await add_member(conn, home, "Tobi", role="child")
        await execute(conn, "update members set preferred_channel = 'whatsapp', presence_token_hash = 'x', "
                            "quiet_start = null, quiet_end = null where id = :id", id=ada)
        await execute(conn, "update households set digest_time = '06:45' where id = :h", h=home.id)
        from_ola = await build_brief(conn, envelope(home, "hi"), NOW)
        from_ada = await build_brief(
            conn, envelope(home, "hi").model_copy(update={"member_id": ada, "member_name": "Ada"}), NOW)
    family = next(line for line in from_ola.splitlines() if line.startswith("Family: "))
    assert set(family.removeprefix("Family: ").split("; ")) == {
        "Ola (adult, set this up, on Telegram)", "Ada (adult, on Telegram and WhatsApp)",
        "Grace (adult, not connected yet)", "Tobi (child)"}
    assert "Ola's setup: no personal link for the shops yet" in from_ola
    assert "Ada's setup: messaged on WhatsApp; has a personal link for the shops" in from_ada
    rhythm = next(line for line in from_ada.splitlines() if line.startswith("Morning brief: "))
    assert rhythm.startswith("Morning brief: 06:45. Quiet hours: Ola 21:30-07:00, ")
    assert "Ada off" in rhythm and "Grace 21:30-07:00" in rhythm and "Tobi" not in rhythm
