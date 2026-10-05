"""ImgBB media storage: images only, and anyone holding the URL can view it (spec section 7.4)."""
import base64

import httpx

from app.core.envelope import MediaRef
from app.media.store import kind_of

UPLOAD = "https://api.imgbb.com/1/upload"
MAX_DAYS = 180   # ImgBB's longest expiration


class MediaError(Exception):
    """An upload or download failed."""


class ImgbbStore:
    def __init__(self, api_key: str, retention_days: int) -> None:
        self._key = api_key
        self._expiration = max(min(retention_days, MAX_DAYS) * 86400, 60)

    async def put(self, household_id: str, message_id: str, n: int, data: bytes, mime: str) -> MediaRef:
        kind = kind_of(mime)
        if kind != "image":
            return MediaRef(kind=kind, mime=mime)   # never persisted here: only a transcript is kept
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(UPLOAD, data={
                    "key": self._key, "image": base64.b64encode(data).decode(), "expiration": str(self._expiration),
                    "name": f"{message_id}-{n}"})
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Never include the exception text: the request carries the API key.
            raise MediaError(f"imgbb upload failed: {type(exc).__name__}") from None
        if response.status_code != 200 or not body.get("success"):
            raise MediaError(f"imgbb upload failed: HTTP {response.status_code}")
        stored = body["data"]
        return MediaRef(kind="image", mime=mime, storage_backend="imgbb", storage_key=stored["id"],
                        storage_url=stored["url"], delete_url=stored["delete_url"])

    async def get(self, ref: MediaRef) -> bytes:
        """The image bytes, fetched here so the LLM provider never needs the public URL."""
        assert ref.storage_url
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            response = await client.get(ref.storage_url)
        if response.status_code != 200:
            raise MediaError(f"imgbb download failed: HTTP {response.status_code}")
        return response.content

    async def delete(self, ref: MediaRef) -> None:
        """ImgBB has no delete call (`delete_url` is a web page); the upload's expiration removes the image."""
