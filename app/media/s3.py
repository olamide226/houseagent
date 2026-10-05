"""S3-compatible media storage (AWS S3, MinIO, R2). A private bucket; nothing is public."""
from typing import Any, Literal

import aioboto3
from aiobotocore.config import AioConfig

from app.core.envelope import MediaRef
from app.media.store import EXTENSIONS, kind_of

# Checksums only where the API requires them: older MinIO and other S3-compatible stores
# reject the trailing checksums newer botocore sends by default.
_CONFIG = AioConfig(request_checksum_calculation="when_required", response_checksum_validation="when_required",
                    retries={"max_attempts": 3, "mode": "standard"})


class S3Store:
    backend: Literal["s3"] = "s3"

    def __init__(self, *, endpoint: str | None, bucket: str, access_key: str, secret_key: str,
                 region: str = "us-east-1") -> None:
        self._bucket = bucket
        self._session = aioboto3.Session(aws_access_key_id=access_key, aws_secret_access_key=secret_key,
                                         region_name=region)
        self._endpoint = endpoint

    def _client(self) -> Any:
        return self._session.client("s3", endpoint_url=self._endpoint, config=_CONFIG)

    async def put(self, household_id: str, message_id: str, n: int, data: bytes, mime: str) -> MediaRef:
        key = f"{household_id}/{message_id}/{n}.{EXTENSIONS.get(mime, 'bin')}"
        async with self._client() as s3:
            await s3.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=mime)
        return MediaRef(kind=kind_of(mime), mime=mime, storage_backend="s3", storage_key=key)

    async def get(self, ref: MediaRef) -> bytes:
        async with self._client() as s3:
            found = await s3.get_object(Bucket=self._bucket, Key=ref.storage_key)
            return bytes(await found["Body"].read())

    async def delete(self, ref: MediaRef) -> None:
        async with self._client() as s3:
            await s3.delete_object(Bucket=self._bucket, Key=ref.storage_key)   # deleting a missing key succeeds
