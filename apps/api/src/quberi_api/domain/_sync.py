"""Directory-sync tracking mixin.

Every entity that can be *mirrored* from an external directory (Feishu,
WeChat Work, DingTalk, SCIM push, ...) carries three fields:

* ``external_id`` — the source system's opaque id for this entity (e.g.
  Feishu ``open_department_id``, DingTalk ``dept_id`` as a string).
* ``external_provider_id`` — Quberi's own id for the ``DirectoryProvider``
  row that produced this record. NULL when ``sync_state = 'local'``.
* ``sync_state`` — see ``domain/enums.SYNC_STATES``.

The pair ``(external_provider_id, external_id)`` is unique wherever the
mixin is applied. Uniqueness is defined per-table because a partial unique
index needs a stable ``__tablename__``.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column


class DirectorySyncMixin:
    """Attach ``external_id`` / ``external_provider_id`` / ``sync_state``."""

    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    # ``external_provider_id`` is a UUID pointing at a future
    # ``directory_providers`` table. That table lands with the sync slice; the
    # column is nullable + unenforced-FK today so we can migrate now without
    # introducing a broken reference.
    external_provider_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    sync_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="local", server_default="local"
    )
