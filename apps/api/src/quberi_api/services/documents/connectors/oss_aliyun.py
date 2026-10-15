"""Aliyun OSS connector — Slice E stub.

Design record for the follow-up:

* ``test_connection`` will call ``oss2.Bucket(auth, endpoint, bucket)
  .head_bucket()``.
* ``list_documents`` will paginate ``list_objects_v2`` with the
  configured prefix; each row carries ``bucket``, ``key``, ``etag``,
  ``last_modified``.
* ``fetch_bytes`` will stream ``get_object`` into MinIO via
  ``local.store`` (mirror policy) or hand off a signed URL directly
  (reference-only policy).
"""

from __future__ import annotations

from quberi_api.services.errors import ServiceError


class OssNotImplemented(ServiceError):
    def __init__(self) -> None:
        super().__init__(
            "the Aliyun OSS connector is not implemented in Slice E; register "
            "the source to reserve the config, but sync is disabled"
        )


async def test_connection(config: dict, credentials: dict | None) -> None:
    _ = (config, credentials)
    raise OssNotImplemented()


async def sync(*args, **kwargs) -> None:
    _ = (args, kwargs)
    raise OssNotImplemented()
