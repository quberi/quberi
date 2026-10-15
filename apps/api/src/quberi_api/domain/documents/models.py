"""Documents + document sources.

Design summary
--------------
Documents live inside a *Space*: the Space's ``RoleBinding`` graph is the
sole permission gate (see Slice B for the evaluator). No per-document
overrides today — if a document needs tighter access, put it in a
tighter space.

A document can arrive via several *sources*, described by
``DocumentSource``:

  * ``local``       — uploaded through our own API into MinIO
  * ``feishu``      — pulled from Feishu Cloud Docs
  * ``oss_aliyun``  — pulled from an Aliyun OSS bucket
  * ``s3_aws``      — pulled from an AWS S3 bucket

Each source picks a ``caching_policy``:
  * ``mirror`` (default): sync copies bytes to MinIO for durability and
    deterministic re-embedding; ``documents.storage_key`` is populated.
  * ``reference_only``: only metadata + ``external_ref`` is stored,
    ``storage_key`` stays NULL, RAG pipeline streams through the
    connector at ingest time.

The Slice-E MVP only ships the ``local`` connector fully; the other three
kinds sit as stubs so the schema and the UI can already talk about them.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from quberi_api.db.base import Base
from quberi_api.db.types import TimestampMixin, UUIDPk
from quberi_api.domain._constraints import in_values
from quberi_api.domain.enums import (
    DOCUMENT_CACHING_POLICY,
    DOCUMENT_INDEXING_STATES,
    DOCUMENT_SOURCE_KINDS,
    DOCUMENT_SOURCE_STATUS,
    DOCUMENT_STATUS,
    DOCUMENT_SYNC_STATES,
)


class DocumentSource(TimestampMixin, Base):
    """A registered connection to an origin of documents.

    Credentials are stored *encrypted* by the service layer (Fernet keyed
    with a per-deployment secret). The ``config`` blob keeps
    non-sensitive knobs (bucket name, region, tenant id, Feishu app_id,
    root folder, prefix, etc.) in plaintext so admins can inspect what's
    hooked up without decrypting anything.

    Every org has exactly one ``kind='local'`` row auto-provisioned on
    demand — it is not stored to disk on bootstrap; the local connector
    is a first-class subject that just happens to run in-process.
    """

    __tablename__ = "document_sources"
    __table_args__ = (
        # One default-local source per org keeps things sane; other kinds
        # can be registered multiple times (multi-tenant Feishu, multi-region
        # buckets, etc.).
        UniqueConstraint(
            "organization_id",
            "kind",
            "name",
            name="uq_document_sources_organization_kind_name",
        ),
        in_values("kind", DOCUMENT_SOURCE_KINDS, name="kind"),
        in_values("caching_policy", DOCUMENT_CACHING_POLICY, name="caching_policy"),
        in_values("status", DOCUMENT_SOURCE_STATUS, name="status"),
        Index("ix_document_sources_organization_id_kind", "organization_id", "kind"),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    credentials_encrypted: Mapped[Optional[bytes]] = mapped_column(
        # ``LargeBinary`` isn't available cleanly cross-dialect; the Fernet
        # ciphertext is ASCII so String(4096) is enough and stays portable.
        String(4096),
        nullable=True,
    )
    caching_policy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="mirror"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active"
    )
    last_synced_at: Mapped[Optional["DateTime"]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class FeishuFolderClaim(Base):
    """Which organisation owns a Feishu folder token.

    All organisations share one Feishu app, and a folder token is not a secret
    (it sits in every share link), so the token alone must not let a second
    organisation pull the same folder. The first organisation to connect a
    folder claims it; another organisation's attempt is refused. The primary
    key makes that atomic even when two registrations race.

    The same organisation may connect one folder to several spaces, so the
    claim is released only when its last source for that folder is removed.
    """

    __tablename__ = "feishu_folder_claims"

    folder_token: Mapped[str] = mapped_column(String(128), primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Document(TimestampMixin, Base):
    """A file that lives inside a space.

    ``storage_key`` is nullable — for ``reference_only`` sources we only
    keep metadata + ``external_ref`` and stream through the connector on
    demand. When populated, the value is a MinIO object key. The default
    key layout is::

        {organization_id}/{space_id}/{document_id}/{filename}

    ``external_ref`` shape depends on ``source_id.kind``::

        feishu:      {"doc_token": "...", "file_type": "docx"}
        oss_aliyun:  {"bucket": "...", "key": "...", "etag": "..."}
        s3_aws:      {"bucket": "...", "key": "...", "version_id": "..."}
        local:       always empty; the storage_key IS the reference.
    """

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint(
            "space_id", "storage_key", name="uq_documents_space_id_storage_key"
        ),
        in_values("status", DOCUMENT_STATUS, name="status"),
        in_values("sync_state", DOCUMENT_SYNC_STATES, name="sync_state"),
        in_values(
            "indexing_state", DOCUMENT_INDEXING_STATES, name="indexing_state"
        ),
        Index("ix_documents_organization_id", "organization_id"),
        Index("ix_documents_space_id_created_at", "space_id", "created_at"),
        Index("ix_documents_uploaded_by_user_id", "uploaded_by_user_id"),
        Index("ix_documents_source_id", "source_id"),
        Index(
            "ix_documents_space_id_indexing_state",
            "space_id",
            "indexing_state",
        ),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    space_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("spaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    source_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("document_sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    uploaded_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    name: Mapped[str] = mapped_column(String(400), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(200), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    checksum_sha256: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    external_ref: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    sync_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="local_only"
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="uploaded"
    )
    attributes: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    # ---- RAG indexing pipeline (Slice G groundwork) ----------------------
    # The RAG worker (later slice) flips these; today every new row lands as
    # ``not_indexed`` with ``chunk_count=0`` so the UI can already surface
    # the state alongside the file.
    indexing_state: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_indexed"
    )
    chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    last_indexed_at: Mapped[Optional["DateTime"]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    indexing_error: Mapped[Optional[str]] = mapped_column(
        String(1024), nullable=True
    )
