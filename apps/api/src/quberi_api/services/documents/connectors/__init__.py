"""Document source connectors.

Each supported ``DocumentSource.kind`` gets a module under this package.

Slice E ships:

* ``local``       — fully implemented; uploads / downloads / deletes go
  straight to our MinIO cluster.
* ``feishu``      — stub. ``test_connection`` returns a "not implemented
  in Slice E" error so the UI can show the source as registered-but-not-
  syncable.
* ``oss_aliyun``  — stub.
* ``s3_aws``      — stub.

The connectors deliberately do NOT share a fat abstract-base-class
hierarchy. Local storage has a fundamentally different API surface
(``upload / download / delete``) than external sources (``test /
list_documents / fetch_bytes``). Trying to force one Protocol on both
just makes the local implementation carry irrelevant methods and vice-
versa. Instead each connector is a plain module with the operations that
make sense for it, and callers dispatch on ``kind``.
"""

from quberi_api.services.documents.connectors import (  # noqa: F401
    feishu,
    local,
    oss_aliyun,
    s3_aws,
)
