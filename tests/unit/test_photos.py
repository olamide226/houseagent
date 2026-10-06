"""Photos and stored media: fetch and store in the pipeline, image input to the model, the
fallbacks when a photo cannot be read, and retention (spec sections 7.2, 7.4, 8.2 and 10)."""
import base64
from datetime import timedelta

from app.agent.loop import MAX_IMAGES, NO_PHOTOS, UNREADABLE, LoopRuntime
from app.core.envelope import Channel
from app.core.timeutil import utcnow
from app.db import execute, fetch_all, tx
from app.llm.types import ImagePart
from app.pipeline import inbound
from app.pipeline import media as media_pipeline
from app.pipeline.inbound import SORRY
from app.worker import jobs
from tests.helpers import (
    FakeAdapter,
    FakeLLM,
    MemoryStore,
    add_item,
    call,
    events_of,
    say,
    seed_home,
    stock_of,
    tg_update,
)

SECRET = {"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"}
PHOTO = [{"file_id": "small", "width": 90, "height": 90}, {"file_id": "RECEIPT", "width": 1280, "height": 960}]


async def rows(sql, **params):
    async with tx() as conn:
        return await fetch_all(conn, sql, **params)


async def send_photo(client, n=1, file_id="RECEIPT", **extra):
    photo = [PHOTO[0], {**PHOTO[1], "file_id": file_id}]
    await client.post("/webhooks/telegram", json=tg_update(n, photo=photo, **extra), headers=SECRET)


async def turn(home, llm, store, adapter=None, stt=None):
    adapter = adapter or FakeAdapter()
    await inbound.process_household(home.id, LoopRuntime(llm, media=store), {Channel.telegram: adapter},
                                    stt=stt, media=store)
    return adapter


def images(llm):
    return [part for part in llm.requests[0][1][-1].content if isinstance(part, ImagePart)]


def text_of(llm):
    return llm.requests[0][1][-1].content[0].text


async def test_a_receipt_photo_is_stored_shown_to_the_model_and_its_items_logged_as_restocked(client):
    async with tx() as conn:
        home = await seed_home(conn)
        await add_item(conn, home, "milk", location="fridge", staple=True, status="out")
    await send_photo(client, caption="tesco shop")
    llm = FakeLLM(call("log_inventory", source="receipt", changes=[
        {"item": "milk", "action": "restocked", "quantity": 2, "unit": "pints"},
        {"item": "bread", "action": "restocked"}]), say("ACK"))
    store = MemoryStore()
    adapter = await turn(home, llm, store)

    (message,) = await rows("select id, media, status from messages where direction = 'in'")
    key = f"{home.id}/{message['id']}/0.jpg"
    assert adapter.fetched == ["RECEIPT"]                                   # the largest size, fetched once
    assert store.objects == {key: b"image:RECEIPT"}
    assert message["media"] == [{"kind": "image", "mime": "image/jpeg", "external_id": "RECEIPT",
                                 "storage_backend": "s3", "storage_key": key}]
    # The model got the stored bytes inline, beside the caption line.
    (image,) = images(llm)
    assert (image.mime, base64.b64decode(image.data_b64)) == ("image/jpeg", b"image:RECEIPT")
    assert text_of(llm) == "tesco shop\n[photo]"                           # Telegram's caption is the message text
    async with tx() as conn:
        assert [(item, kind, source) for item, kind, _, source in await events_of(conn, home)] == [
            ("milk", "restocked", "receipt"), ("bread", "restocked", "receipt")]
        assert (await stock_of(conn, home))[("milk", "fridge")][1] == "in_stock"


async def test_a_model_without_image_input_is_told_a_photo_came_and_sees_none(client):
    async with tx() as conn:
        home = await seed_home(conn)
    await send_photo(client)
    llm = FakeLLM(say("I can't read photos with this model, sorry."), supports_images=False)
    store = MemoryStore()
    await turn(home, llm, store)
    assert images(llm) == [] and text_of(llm).endswith(NO_PHOTOS)
    (reply,) = await rows("select text from outbox")
    assert reply["text"] == "I can't read photos with this model, sorry."
    assert await rows("select 1 from inventory_events") == []


async def test_a_photo_that_cannot_be_stored_or_loaded_does_not_fail_the_turn(client):
    async with tx() as conn:
        home = await seed_home(conn)
    await send_photo(client, 1, caption="the fridge")
    llm = FakeLLM(say("I couldn't open that photo, can you send it again?"))
    await turn(home, llm, None)                                             # no media backend configured
    assert images(llm) == [] and text_of(llm).endswith(UNREADABLE)

    await send_photo(client, 2)
    llm = FakeLLM(say("I couldn't open that photo."))
    await turn(home, llm, MemoryStore(fail=RuntimeError("bucket unreachable")))
    assert images(llm) == [] and text_of(llm).endswith(UNREADABLE)

    await send_photo(client, 3)
    llm = FakeLLM(say("I couldn't open that photo."))
    await turn(home, llm, MemoryStore(), FakeAdapter(fail=RuntimeError("telegram down")))
    assert images(llm) == [] and text_of(llm).endswith(UNREADABLE)

    messages = await rows("select status, media from messages where direction = 'in' order by created_at")
    assert [m["status"] for m in messages] == ["processed"] * 3
    assert all("storage_key" not in m["media"][0] for m in messages)
    assert SORRY not in [r["text"] for r in await rows("select text from outbox")]


async def test_at_most_four_photos_reach_the_model_and_the_rest_are_mentioned(client):
    async with tx() as conn:
        home = await seed_home(conn)
    for n in range(1, 7):
        await send_photo(client, n, file_id=f"P{n}")
    llm = FakeLLM(say("NOOP"))
    store = MemoryStore()
    await turn(home, llm, store)
    assert [base64.b64decode(i.data_b64) for i in images(llm)] == [f"image:P{n}".encode() for n in range(1, 5)]
    assert len(store.objects) == 6 and MAX_IMAGES == 4
    assert "2 more photos not read" in text_of(llm)


async def test_what_was_stored_stays_recorded_when_the_turn_fails(client):
    async with tx() as conn:
        home = await seed_home(conn)
    await send_photo(client)
    store = MemoryStore()
    await turn(home, FakeLLM(), store)                                      # the model call fails
    (message,) = await rows("select status, media from messages where direction = 'in'")
    assert message["status"] == "failed"
    assert list(store.objects) == [message["media"][0]["storage_key"]]      # retention can still find it


async def test_a_voice_note_is_fetched_once_then_stored_and_transcribed(client):
    class StandInTranscriber:
        async def transcribe(self, audio: bytes, mime: str) -> str:
            return "we're out of eggs"

    async with tx() as conn:
        home = await seed_home(conn)
    await client.post("/webhooks/telegram", headers=SECRET,
                      json=tg_update(1, voice={"file_id": "V1", "mime_type": "audio/ogg", "duration": 3}))
    llm = FakeLLM(say("NOOP"))
    store = MemoryStore()
    adapter = await turn(home, llm, store, stt=StandInTranscriber())
    (message,) = await rows("select id, media from messages")
    assert adapter.fetched == ["V1"]
    assert store.objects == {f"{home.id}/{message['id']}/0.ogg": b"audio-bytes"}
    # Preparing the same message again fetches and stores nothing.
    assert not await media_pipeline.prepare(message["media"], home.id, message["id"], adapter, StandInTranscriber(), store)
    assert adapter.fetched == ["V1"]
    assert message["media"][0]["transcript"] == "we're out of eggs"
    assert text_of(llm) == "[voice note] we're out of eggs" and images(llm) == []


# ---------------------------------------------------------------- retention
async def test_media_older_than_the_retention_is_deleted_and_only_the_words_remain(client):
    async with tx() as conn:
        home = await seed_home(conn)
    await send_photo(client, 1, caption="old receipt")
    await send_photo(client, 2, caption="new receipt")
    await client.post("/webhooks/telegram", headers=SECRET,
                      json=tg_update(3, voice={"file_id": "V1", "mime_type": "audio/ogg", "duration": 3}))

    class StandInTranscriber:
        async def transcribe(self, audio: bytes, mime: str) -> str:
            return "an old voice note"

    store = MemoryStore()
    await turn(home, FakeLLM(say("NOOP")), store, stt=StandInTranscriber())
    async with tx() as conn:
        await execute(conn, "update messages set created_at = created_at - interval '91 days' "
                            "where external_id in ('1', '3')")
    old_photo, new_photo, old_voice = (m["media"][0] for m in await rows(
        "select media from messages where direction = 'in' order by external_id"))
    now = utcnow()

    assert await jobs.media_cleanup(store, 90, now) == 2
    assert sorted(store.deleted) == sorted([old_photo["storage_key"], old_voice["storage_key"]])
    assert list(store.objects) == [new_photo["storage_key"]]
    kept = {m["external_id"]: m["media"][0] for m in await rows(
        "select external_id, media from messages where direction = 'in'")}
    assert [m["text"] for m in await rows("select text from messages where external_id = '1'")] == ["old receipt"]
    assert kept["1"] == {"kind": "image", "mime": "image/jpeg", "external_id": "RECEIPT"}
    assert kept["3"]["transcript"] == "an old voice note" and "storage_key" not in kept["3"]
    assert kept["2"] == new_photo

    assert await jobs.media_cleanup(store, 90, now) == 0                    # a second run finds nothing
    assert len(store.deleted) == 2
    assert await jobs.media_cleanup(store, 90, now + timedelta(days=90)) == 1   # the newer one, in its turn


async def test_cleanup_keeps_the_reference_when_the_store_is_down_and_leaves_other_backends_alone(client):
    async with tx() as conn:
        home = await seed_home(conn)
    await send_photo(client)
    store = MemoryStore()
    await turn(home, FakeLLM(say("NOOP")), store)
    async with tx() as conn:
        await execute(conn, "update messages set created_at = created_at - interval '200 days'")
    (before,) = await rows("select media from messages where direction = 'in'")

    down = MemoryStore(fail=RuntimeError("bucket unreachable"))
    assert await jobs.media_cleanup(down, 90) == 0
    other = MemoryStore()
    other.backend = "imgbb"
    assert await jobs.media_cleanup(other, 90) == 0 and other.deleted == []
    assert await rows("select media from messages where direction = 'in'") == [before]

    assert await jobs.media_cleanup(store, 90) == 1 and store.objects == {}
