"""Object storage for original files.

The text OpsPilot indexes lives in Postgres; the files it came from are kept here, so they can
be downloaded as they were and processed again (by a better parser, a new chunker) without
asking anyone to upload them again.

- `none`: no copies kept (the default for tests and the smallest setups)
- `local`: a directory on disk (development without Docker)
- `s3`: Amazon S3, or anything that speaks its API: MinIO locally (`docker compose up minio`),
  Cloudflare R2, Backblaze B2. Credentials come from the usual AWS settings.

Keys look like `workspaces/<workspace>/documents/<document>/<sha256>`: the content hash in the
key means a changed file never overwrites the previous version's object.
"""

import asyncio
import hashlib
import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from opspilot.config import Settings

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """The store couldn't be reached, or refused the operation."""


class ObjectStore(Protocol):
    name: str

    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes | None:
        """The object, or None if there's no such key."""
        ...

    async def delete(self, key: str) -> None:
        """Deletes it; deleting a key that doesn't exist is fine."""
        ...


def document_key(workspace_id: uuid.UUID, document_id: uuid.UUID, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    return f"workspaces/{workspace_id}/documents/{document_id}/{digest}"


class NoStore:
    name = "none"

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        return None

    async def get(self, key: str) -> bytes | None:
        return None

    async def delete(self, key: str) -> None:
        return None


class LocalStore:
    name = "local"

    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        # Keys are built by us, but a key must never point outside the root
        if not path.is_relative_to(self.root):
            raise StorageError(f"Key outside the storage directory: {key}")
        if os.name == "nt":
            # Keys are long (two uuids and a sha256): past Windows' 260-character MAX_PATH
            # in all but the shortest directories. The \\?\ prefix lifts the limit.
            return Path("\\\\?\\" + str(path))
        return path

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Written whole, then renamed: a reader never sees half a file
            partial = path.with_suffix(".partial")
            partial.write_bytes(data)
            partial.replace(path)

        await asyncio.to_thread(write)

    async def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return await asyncio.to_thread(lambda: path.read_bytes() if path.exists() else None)

    async def delete(self, key: str) -> None:
        path = self._path(key)
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))


class S3Store:
    """boto3 is synchronous, so each call runs in a thread; the client is thread-safe."""

    name = "s3"

    def __init__(self, client: "S3Client", bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    @classmethod
    def from_settings(cls, settings: Settings) -> "S3Store":
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=(
                settings.s3_secret_access_key.get_secret_value()
                if settings.s3_secret_access_key
                else None
            ),
            config=Config(
                # MinIO and most S3-compatible stores want path-style URLs
                s3={"addressing_style": "path" if settings.s3_endpoint_url else "auto"},
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=30,
            ),
        )
        return cls(client, settings.s3_bucket)

    async def _call(self, fn: Any, **kwargs: Any) -> Any:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return await asyncio.to_thread(fn, Bucket=self.bucket, **kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "404"):
                return None
            raise StorageError(f"S3 refused it: {code or exc}") from exc
        except BotoCoreError as exc:
            raise StorageError(f"S3 unreachable: {exc}") from exc

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await self._call(
            self.client.put_object,
            Key=key,
            Body=data,
            ContentType=content_type,
            # Integrity end to end: S3 rejects the upload if the bytes arrive changed
            ChecksumAlgorithm="SHA256",
        )

    async def get(self, key: str) -> bytes | None:
        response = await self._call(self.client.get_object, Key=key)
        if response is None:
            return None
        body = response["Body"]
        data: bytes = await asyncio.to_thread(body.read)
        return data

    async def delete(self, key: str) -> None:
        await self._call(self.client.delete_object, Key=key)

    async def ensure_bucket(self) -> None:
        """For development (MinIO): creates the bucket if it isn't there."""
        from botocore.exceptions import ClientError

        try:
            await asyncio.to_thread(self.client.head_bucket, Bucket=self.bucket)
        except ClientError:
            await asyncio.to_thread(self.client.create_bucket, Bucket=self.bucket)


def build_store(settings: Settings) -> ObjectStore:
    if settings.storage_backend == "s3":
        return S3Store.from_settings(settings)
    if settings.storage_backend == "local":
        return LocalStore(settings.storage_local_dir)
    return NoStore()
