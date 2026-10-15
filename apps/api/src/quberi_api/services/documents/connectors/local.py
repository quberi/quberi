"""Local object-store connector, backed by our MinIO cluster.

Exposes three verbs used by ``services.documents.core``:

* ``store`` — stream a file-like into MinIO, returning the finalised
  storage key + sha256.
* ``open_stream`` — read a MinIO object back as an iterator of bytes,
  suitable for FastAPI's ``StreamingResponse``.
* ``delete`` — remove an object; a missing object is treated as success
  because deletion is idempotent by design.

``ensure_bucket`` is called at process boot from
``services.documents.core`` on the first upload; subsequent calls are
cheap because the SDK just gets a HEAD-bucket 200. All MinIO calls are
synchronous; we wrap them in ``asyncio.to_thread`` to keep the FastAPI
event loop free.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import tempfile
from dataclasses import dataclass
from typing import AsyncIterator, BinaryIO

from minio import Minio
from minio.error import S3Error

from quberi_api.core.config import Settings, get_settings
from quberi_api.infrastructure.object_storage import create_object_storage_client


# ---------------------------------------------------------------------------
# Result payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredObject:
    storage_key: str
    size_bytes: int
    checksum_sha256: str


# ---------------------------------------------------------------------------
# Client access
# ---------------------------------------------------------------------------


_client_lock = asyncio.Lock()
_client_cache: tuple[Minio, Settings] | None = None
_bucket_ensured = False


async def _client() -> tuple[Minio, Settings]:
    """Return a shared ``Minio`` client, creating it on first use.

    We can't reuse the app-startup registry client from a service module
    without pulling FastAPI's dependency graph in here, so we build a
    module-scoped singleton. It's safe: ``Minio`` is thread-safe by
    documentation and we only mutate ``_client_cache`` under the lock.
    """

    global _client_cache
    async with _client_lock:
        if _client_cache is None:
            settings = get_settings()
            _client_cache = (create_object_storage_client(settings), settings)
        return _client_cache


async def ensure_bucket() -> str:
    """Idempotent make-bucket. Returns the bucket name.

    The ``minio-init`` docker service creates it at boot, so under
    normal operation this is a HEAD 200. We still call it defensively
    for cases where the API image runs against a fresh MinIO
    (integration tests, one-off dev boxes).
    """

    global _bucket_ensured
    client, settings = await _client()
    bucket = settings.minio_bucket
    if _bucket_ensured:
        return bucket

    def _do_ensure() -> None:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)

    await asyncio.to_thread(_do_ensure)
    _bucket_ensured = True
    return bucket


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


async def store(
    *,
    storage_key: str,
    source_stream: BinaryIO,
    content_type: str,
    max_bytes: int,
) -> StoredObject:
    """Read ``source_stream`` fully, hash + upload to MinIO.

    ``source_stream`` is read once into a spooled temp file so we can
    compute SHA256 and hand the same buffer to MinIO without spooling to
    memory. This is the right trade-off for ≤ 25 MB uploads today; if we
    raise the limit past a few hundred MB we should switch to a
    streaming hash + multipart upload.

    Raises ``DocumentValidationError`` if the payload exceeds
    ``max_bytes``.
    """

    from quberi_api.services.documents.errors import DocumentValidationError

    bucket = await ensure_bucket()
    hasher = hashlib.sha256()

    # SpooledTemporaryFile keeps small files in RAM (fast) and rolls the
    # rest to disk so we don't blow up on the max size.
    scratch = tempfile.SpooledTemporaryFile(
        max_size=8 * 1024 * 1024,  # 8 MiB before spilling to disk
    )
    total = 0
    try:
        while True:
            chunk = source_stream.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise DocumentValidationError(
                    f"upload exceeds the {max_bytes}-byte limit"
                )
            hasher.update(chunk)
            scratch.write(chunk)
        scratch.seek(0)

        client, _ = await _client()

        def _put() -> None:
            client.put_object(
                bucket_name=bucket,
                object_name=storage_key,
                data=scratch,
                length=total,
                content_type=content_type,
            )

        await asyncio.to_thread(_put)
    finally:
        scratch.close()

    return StoredObject(
        storage_key=storage_key,
        size_bytes=total,
        checksum_sha256=hasher.hexdigest(),
    )


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


async def open_stream(storage_key: str) -> AsyncIterator[bytes]:
    """Yield MinIO object bytes in 1 MiB chunks.

    Callers should ``async for chunk in open_stream(...): yield chunk``
    to hand off directly to FastAPI's ``StreamingResponse``. The
    underlying ``get_object`` returns a urllib3 ``HTTPResponse``; we
    iterate it on a worker thread and hand chunks back via a queue.
    """

    client, settings = await _client()
    bucket = settings.minio_bucket

    def _open() -> "urllib3.response.HTTPResponse":  # noqa: F821
        return client.get_object(bucket, storage_key)

    response = await asyncio.to_thread(_open)

    async def _iter() -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await asyncio.to_thread(response.read, 1024 * 1024)
                if not chunk:
                    return
                yield chunk
        finally:
            await asyncio.to_thread(response.close)
            await asyncio.to_thread(response.release_conn)

    async for c in _iter():
        yield c


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def delete(storage_key: str) -> None:
    """Remove an object. Missing objects are treated as success."""

    client, settings = await _client()
    bucket = settings.minio_bucket

    def _do_delete() -> None:
        try:
            client.remove_object(bucket, storage_key)
        except S3Error as exc:
            if exc.code in {"NoSuchKey", "NoSuchObject"}:
                return
            raise

    await asyncio.to_thread(_do_delete)


# ---------------------------------------------------------------------------
# Helpers used by the service layer
# ---------------------------------------------------------------------------


def storage_key_for(
    *, organization_id: str, space_id: str, document_id: str, filename: str
) -> str:
    """Deterministic key layout ``{org}/{space}/{doc}/{filename}``.

    Keeping the filename in the key is optional but useful: presigned or
    proxied downloads honour ``response-content-disposition`` correctly,
    and admins poking around the bucket can eyeball what a blob is.
    """

    # Very light sanitisation — dropping slashes is enough because the
    # rest of the key is machine-generated uuids we control.
    safe = filename.replace("/", "_").replace("\\", "_").strip() or "file"
    return f"{organization_id}/{space_id}/{document_id}/{safe}"


# Re-export a small BytesIO helper so callers can wrap a bytes payload.
def wrap_bytes(payload: bytes) -> io.BytesIO:
    buf = io.BytesIO(payload)
    return buf
