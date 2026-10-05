"""MediaStore protocol and factory (spec section 7.4). The pipeline and the agent only ever
see MediaRef, so the backend is one environment variable."""
from typing import Literal, Protocol

from app.config import Settings
from app.core.envelope import MediaRef

EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif",
              "audio/ogg": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/wav": "wav",
              "audio/x-caf": "caf", "application/pdf": "pdf"}


def kind_of(mime: str) -> Literal["image", "audio", "video", "document"]:
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    return "video" if mime.startswith("video/") else "document"


class MediaStore(Protocol):
    @property
    def backend(self) -> Literal["s3", "imgbb"]: ...

    async def put(self, household_id: str, message_id: str, n: int, data: bytes, mime: str) -> MediaRef:
        """Store one attachment. The returned ref has no storage fields if the backend does not keep this kind."""
        ...

    async def get(self, ref: MediaRef) -> bytes: ...
    async def delete(self, ref: MediaRef) -> None: ...


def make_media_store(settings: Settings) -> MediaStore | None:
    """The configured backend, or None while its variables are unset: photos then go unread."""
    if settings.media_backend == "imgbb":
        if not settings.imgbb_api_key:
            return None
        from app.media.imgbb import ImgbbStore

        return ImgbbStore(settings.imgbb_api_key, settings.media_retention_days)
    if not (settings.s3_bucket and settings.s3_access_key and settings.s3_secret_key):
        return None
    from app.media.s3 import S3Store

    return S3Store(endpoint=settings.s3_endpoint, bucket=settings.s3_bucket, access_key=settings.s3_access_key,
                   secret_key=settings.s3_secret_key, region=settings.s3_region)
