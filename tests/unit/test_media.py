"""MediaStore backends (spec section 7.4): S3 against a local stand-in server, ImgBB against mocked HTTP."""
import asyncio
import base64

import httpx
import pytest
import respx
import uvicorn

from app.config import Settings
from app.core.envelope import MediaRef
from app.media.imgbb import UPLOAD, ImgbbStore, MediaError
from app.media.s3 import S3Store
from app.media.store import make_media_store

JPEG = b"\xff\xd8\xff\xe0 not really a jpeg"
BASE = dict(database_url="postgresql+asyncpg://x/y", public_base_url="http://x", session_secret="s",
            llm_provider="openai_compat", llm_model="m", _env_file=None)


@pytest.fixture
async def s3():
    """A minimal S3: PUT, GET and DELETE on /bucket/key, kept in a dict. Yields (store, objects, requests)."""
    objects: dict[str, tuple[bytes, str]] = {}
    requests: list[tuple[str, str, dict[str, str]]] = []

    async def app(scope, receive, send):
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        headers = {k.decode(): v.decode() for k, v in scope["headers"]}
        method, path = scope["method"], scope["path"]
        requests.append((method, path, headers))
        status, payload, content_type = 200, b"", "application/xml"
        if method == "PUT":
            objects[path] = (body, headers.get("content-type", ""))
        elif method == "GET" and path in objects:
            payload, content_type = objects[path]
        elif method == "GET":
            status, payload = 404, b"<Error><Code>NoSuchKey</Code><Message>missing</Message></Error>"
        elif method == "DELETE":
            status = 204
            objects.pop(path, None)
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", content_type.encode()), (b"content-length", b"%d" % len(payload))]})
        await send({"type": "http.response.body", "body": payload})

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="off"))
    task = asyncio.create_task(server.serve())
    while not server.started:   # noqa: ASYNC110 - uvicorn exposes a flag, not an event
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    store = S3Store(endpoint=f"http://127.0.0.1:{port}", bucket="media", access_key="test-access",
                    secret_key="test-secret")
    yield store, objects, requests
    server.should_exit = True
    await task


async def test_s3_stores_under_household_message_and_index_then_reads_and_deletes(s3):
    store, objects, requests = s3
    ref = await store.put("house-1", "msg-9", 0, JPEG, "image/jpeg")
    assert ref == MediaRef(kind="image", mime="image/jpeg", storage_backend="s3", storage_key="house-1/msg-9/0.jpg")
    assert objects == {"/media/house-1/msg-9/0.jpg": (JPEG, "image/jpeg")}
    assert requests[0][2]["authorization"].startswith("AWS4-HMAC-SHA256 Credential=test-access/")   # signed, private

    voice = await store.put("house-1", "msg-9", 1, b"ogg-bytes", "audio/ogg")
    assert (voice.kind, voice.storage_key) == ("audio", "house-1/msg-9/1.ogg")       # S3 keeps audio too

    assert await store.get(ref) == JPEG
    await store.delete(ref)
    assert list(objects) == ["/media/house-1/msg-9/1.ogg"]
    await store.delete(ref)                                                          # already gone: still fine
    with pytest.raises(Exception, match="NoSuchKey"):
        await store.get(ref)


@respx.mock
async def test_imgbb_uploads_images_with_a_capped_expiration_and_reads_them_back():
    upload = respx.post(UPLOAD).mock(return_value=httpx.Response(200, json={"success": True, "data": {
        "id": "2ndCYJK", "url": "https://i.ibb.co/w04Prt6/receipt.jpg", "delete_url": "https://ibb.co/2ndCYJK/670a"}}))
    download = respx.get("https://i.ibb.co/w04Prt6/receipt.jpg").mock(return_value=httpx.Response(200, content=JPEG))

    ref = await ImgbbStore("imgbb-key", retention_days=365).put("house-1", "msg-9", 0, JPEG, "image/jpeg")
    assert ref == MediaRef(kind="image", mime="image/jpeg", storage_backend="imgbb", storage_key="2ndCYJK",
                           storage_url="https://i.ibb.co/w04Prt6/receipt.jpg",
                           delete_url="https://ibb.co/2ndCYJK/670a")
    form = dict(httpx.QueryParams(upload.calls.last.request.content.decode()))
    assert form["key"] == "imgbb-key" and base64.b64decode(form["image"]) == JPEG
    assert form["expiration"] == str(180 * 86400)                # 365 days asked, ImgBB allows 180

    await ImgbbStore("imgbb-key", retention_days=30).put("house-1", "msg-9", 1, JPEG, "image/jpeg")
    assert dict(httpx.QueryParams(upload.calls.last.request.content.decode()))["expiration"] == str(30 * 86400)

    assert await ImgbbStore("imgbb-key", 90).get(ref) == JPEG
    assert download.call_count == 1


@respx.mock
async def test_imgbb_never_persists_audio_and_reports_failures_without_the_key():
    upload = respx.post(UPLOAD).mock(return_value=httpx.Response(400, json={"success": False}))
    store = ImgbbStore("imgbb-secret-key", 90)

    voice = await store.put("house-1", "msg-9", 0, b"ogg-bytes", "audio/ogg")
    assert voice == MediaRef(kind="audio", mime="audio/ogg") and upload.call_count == 0

    with pytest.raises(MediaError) as refused:
        await store.put("house-1", "msg-9", 1, JPEG, "image/jpeg")
    upload.mock(side_effect=httpx.ConnectError("boom key=imgbb-secret-key"))
    with pytest.raises(MediaError) as unreachable:
        await store.put("house-1", "msg-9", 1, JPEG, "image/jpeg")
    assert "imgbb-secret-key" not in f"{refused.value} {unreachable.value}"

    respx.get("https://i.ibb.co/gone.jpg").mock(return_value=httpx.Response(404))
    with pytest.raises(MediaError):
        await store.get(MediaRef(kind="image", storage_backend="imgbb", storage_url="https://i.ibb.co/gone.jpg"))


def test_the_backend_is_one_setting_and_absent_until_its_variables_are_set():
    assert make_media_store(Settings(**BASE)) is None                                   # s3 by default, unset
    assert make_media_store(Settings(**BASE, media_backend="imgbb")) is None
    assert isinstance(make_media_store(Settings(**BASE, media_backend="imgbb", imgbb_api_key="k")), ImgbbStore)
    assert isinstance(make_media_store(Settings(**BASE, s3_bucket="b", s3_access_key="a", s3_secret_key="s",
                                                s3_endpoint="http://minio:9000")), S3Store)
