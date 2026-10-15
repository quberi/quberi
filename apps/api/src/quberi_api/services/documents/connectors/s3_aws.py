"""AWS S3 connector — Slice E stub.

Design record for the follow-up:

* Auth via ``boto3.session.Session(aws_access_key_id, aws_secret_access_key,
  region_name)``; STS/AssumeRole for prod tenants.
* ``test_connection`` = ``head_bucket``.
* ``list_documents`` = ``list_objects_v2`` (or ``ListObjectVersions`` for
  versioned buckets); pagination via ``ContinuationToken``.
* ``fetch_bytes`` = ``get_object`` streamed into MinIO by mirror policy,
  or ``generate_presigned_url`` in reference-only mode.
"""

from __future__ import annotations

from quberi_api.services.errors import ServiceError


class S3NotImplemented(ServiceError):
    def __init__(self) -> None:
        super().__init__(
            "the AWS S3 connector is not implemented in Slice E; register "
            "the source to reserve the config, but sync is disabled"
        )


async def test_connection(config: dict, credentials: dict | None) -> None:
    _ = (config, credentials)
    raise S3NotImplemented()


async def sync(*args, **kwargs) -> None:
    _ = (args, kwargs)
    raise S3NotImplemented()
