"""Invite codes and dashboard login tokens: format, expiry, single use, limits."""
import re
from datetime import timedelta

from app.core.envelope import Channel
from app.core.identity import hash_token, member_for_handle, new_invite_code, parse_invite_code, redeem_invite
from app.core.timeutil import utcnow
from app.db import fetch_one, tx
from app.services import members
from tests.helpers import add_member, seed_home


def test_invite_codes_are_four_letters_hyphen_four_alphanumerics_without_lookalikes():
    codes = {new_invite_code() for _ in range(500)}
    assert len(codes) > 490
    for code in codes:
        assert re.fullmatch(r"[A-Z]{4}-[A-Z0-9]{4}", code)
        assert not set(code) & set("0O1I")


def test_only_a_whole_message_that_is_a_code_counts():
    assert parse_invite_code(" abcd-2x9z ") == "ABCD-2X9Z"
    for text in (None, "", "ABCD2X9Z", "my code is ABCD-2X9Z", "ABC-2X9Z", "AB1D-2X9Z", "ABCD-2X9ZZ"):
        assert parse_invite_code(text) is None


async def test_invite_is_stored_hashed_and_links_the_first_channel_as_preferred():
    now = utcnow()
    async with tx() as conn:
        home = await seed_home(conn, telegram_id=None)
        code = await members.create_invite(conn, home.ola, now)
        stored = await fetch_one(conn, "select invite_code_hash, invite_expires_at from members where id = :id", id=home.ola)
        assert stored["invite_code_hash"] == hash_token(code) and code not in stored["invite_code_hash"]
        assert stored["invite_expires_at"] == now + timedelta(days=7)

        member = await redeem_invite(conn, Channel.telegram, "1001", code, now)
        assert member["id"] == home.ola and member["name"] == "Ola"
        assert (await member_for_handle(conn, Channel.telegram, "1001"))["id"] == home.ola
        preferred = await fetch_one(conn, "select preferred_channel from members where id = :id", id=home.ola)
        assert preferred["preferred_channel"] == "telegram"


async def test_a_code_works_once_per_channel_so_one_invite_links_every_channel():
    now = utcnow()
    async with tx() as conn:
        home = await seed_home(conn, telegram_id=None)
        code = await members.create_invite(conn, home.ola, now)
        assert await redeem_invite(conn, Channel.telegram, "1001", code, now)
        assert await redeem_invite(conn, Channel.telegram, "9999", code, now) is None    # same channel again
        assert await redeem_invite(conn, Channel.whatsapp, "+447700900001", code, now)   # another channel
        preferred = await fetch_one(conn, "select preferred_channel from members where id = :id", id=home.ola)
        assert preferred["preferred_channel"] == "telegram"                              # first one stays


async def test_expired_wrong_and_replaced_codes_are_refused():
    now = utcnow()
    async with tx() as conn:
        home = await seed_home(conn, telegram_id=None)
        old = await members.create_invite(conn, home.ola, now)
        assert await redeem_invite(conn, Channel.telegram, "1001", "ZZZZ-9999", now) is None
        assert await redeem_invite(conn, Channel.telegram, "1001", old, now + timedelta(days=7, seconds=1)) is None
        new = await members.create_invite(conn, home.ola, now)
        assert await redeem_invite(conn, Channel.telegram, "1001", old, now) is None
        assert await redeem_invite(conn, Channel.telegram, "1001", new, now + timedelta(days=6, hours=23))


async def test_a_handle_already_linked_to_someone_cannot_claim_another_member():
    now = utcnow()
    async with tx() as conn:
        home = await seed_home(conn, telegram_id="1001")
        ada = await add_member(conn, home, "Ada")
        code = await members.create_invite(conn, ada, now)
        assert await redeem_invite(conn, Channel.telegram, "1001", code, now) is None
        assert (await member_for_handle(conn, Channel.telegram, "1001"))["id"] == home.ola


async def test_login_tokens_are_single_use_expire_in_ten_minutes_and_are_stored_hashed():
    now = utcnow()
    async with tx() as conn:
        home = await seed_home(conn)
        token = await members.create_login_token(conn, home.ola, now)
        stored = await fetch_one(conn, "select token_hash, expires_at from login_tokens")
        assert stored == {"token_hash": hash_token(token), "expires_at": now + timedelta(minutes=10)}
        assert await members.consume_login_token(conn, "not-the-token", now) is None
        assert (await members.consume_login_token(conn, token, now))["id"] == home.ola
        assert await members.consume_login_token(conn, token, now) is None

        late = await members.create_login_token(conn, home.ola, now)
        assert await members.consume_login_token(conn, late, now + timedelta(minutes=10, seconds=1)) is None


async def test_login_links_are_limited_to_five_an_hour_and_never_given_to_children():
    now = utcnow()
    async with tx() as conn:
        home = await seed_home(conn)
        tobi = await add_member(conn, home, "Tobi", role="child")
        assert await members.create_login_token(conn, tobi, now) is None
        assert all([await members.create_login_token(conn, home.ola, now) for _ in range(5)])
        assert await members.create_login_token(conn, home.ola, now) is None
        assert await members.create_login_token(conn, home.ola, now + timedelta(hours=1, seconds=1))
