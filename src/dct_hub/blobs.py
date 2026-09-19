"""Artifact blob storage: local filesystem (atomic) or S3-compatible (HA).

The publish invariant is `put_file` atomicity: a reader sees the previous blob
or the new one, never a torn write (os.replace locally; a PUT is atomic in S3).
Renders always land in a local staging path first — dct needs a filesystem
`--output` — and `put_file` publishes from there. Locators are backend-neutral
(`<board>/<key>.<fmt>`): a relative path locally, an object key suffix in S3.
"""

import os
import re
import uuid
from pathlib import Path
from typing import AsyncIterator, Protocol


class BlobMissing(Exception):
    pass


def artifact_locator(board: str, key: str, fmt: str) -> str:
    if not re.fullmatch(r"[a-z0-9]+", fmt):
        raise ValueError(f"unsafe artifact format: {fmt!r}")
    parts = board.split("/")
    if board.startswith("/") or "\\" in board or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"unsafe artifact board: {board!r}")
    return f"{board}/{key}.{fmt}"


class BlobStore(Protocol):
    def staging_path(self, locator: str) -> Path:
        """Local path for the render to write; must be same-FS as the backend
        for local stores so `put_file` can rename atomically."""
        ...

    async def put_file(self, locator: str, tmp: Path) -> None:
        """Atomically publish the staged file under locator."""
        ...

    async def exists(self, locator: str) -> bool: ...

    async def read(self, locator: str) -> AsyncIterator[bytes]:
        """Stream the blob; raises BlobMissing."""
        ...

    async def delete(self, locator: str) -> None: ...


class LocalBlobs:
    def __init__(self, root: Path):
        self.root = root
        self.staging_dir = root / ".staging"
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self._resolved_root = root.resolve()

    def _path(self, locator: str) -> Path:
        # Locators come from our own metadata, but never trust them blindly:
        # a planted absolute or ../ locator must not escape the store root.
        path = (self.root / locator).resolve()
        if not path.is_relative_to(self._resolved_root):
            raise ValueError(f"unsafe blob locator: {locator!r}")
        return path

    def staging_path(self, locator: str) -> Path:
        return self.staging_dir / f"{uuid.uuid4().hex}.tmp"

    async def put_file(self, locator: str, tmp: Path) -> None:
        dest = self._path(locator)
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, dest)

    async def exists(self, locator: str) -> bool:
        return self._path(locator).exists()

    async def read(self, locator: str) -> AsyncIterator[bytes]:
        try:
            with self._path(locator).open("rb") as fh:
                while chunk := fh.read(65536):
                    yield chunk
        except FileNotFoundError:
            raise BlobMissing(locator)

    async def delete(self, locator: str) -> None:
        self._path(locator).unlink(missing_ok=True)


class S3Blobs:
    """aiobotocore-backed store. Imported lazily: the `ha` extra is only
    required when HA storage is actually configured."""

    def __init__(self, bucket: str, prefix: str, staging_dir: Path, endpoint_url: str | None = None, region: str | None = None):
        try:
            import aiobotocore.session  # noqa: F401
        except ImportError as exc:
            raise RuntimeError("S3 storage requires the ha extra: pip install 'dct-hub[ha]'") from exc
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.staging_dir = staging_dir
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.endpoint_url = endpoint_url
        self.region = region

    def _session(self):
        import aiobotocore.session

        return aiobotocore.session.get_session()

    def _key(self, locator: str) -> str:
        if locator.startswith("/") or any(p in ("", ".", "..") for p in locator.split("/")):
            raise ValueError(f"unsafe blob locator: {locator!r}")
        return f"{self.prefix}/{locator}" if self.prefix else locator

    def staging_path(self, locator: str) -> Path:
        return self.staging_dir / f"{uuid.uuid4().hex}.tmp"

    def _client_kwargs(self) -> dict:
        kwargs: dict = {}
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url
        if self.region:
            kwargs["region_name"] = self.region
        return kwargs

    async def put_file(self, locator: str, tmp: Path) -> None:
        async with self._session().create_client("s3", **self._client_kwargs()) as client:
            with tmp.open("rb") as fh:
                await client.put_object(Bucket=self.bucket, Key=self._key(locator), Body=fh.read())
        tmp.unlink(missing_ok=True)

    async def exists(self, locator: str) -> bool:
        from botocore.exceptions import ClientError

        async with self._session().create_client("s3", **self._client_kwargs()) as client:
            try:
                await client.head_object(Bucket=self.bucket, Key=self._key(locator))
                return True
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                    return False
                raise

    async def read(self, locator: str) -> AsyncIterator[bytes]:
        from botocore.exceptions import ClientError

        async with self._session().create_client("s3", **self._client_kwargs()) as client:
            try:
                resp = await client.get_object(Bucket=self.bucket, Key=self._key(locator))
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                    raise BlobMissing(locator)
                raise
            async with resp["Body"] as stream:
                while True:
                    chunk = await stream.read(65536)
                    if not chunk:
                        break
                    yield chunk

    async def delete(self, locator: str) -> None:
        async with self._session().create_client("s3", **self._client_kwargs()) as client:
            await client.delete_object(Bucket=self.bucket, Key=self._key(locator))
