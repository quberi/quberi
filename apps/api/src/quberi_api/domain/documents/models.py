"""Documents + document sources.

Design summary
--------------
Documents live inside a *Space*: the Space's ``RoleBinding`` graph decides who
can read them (see Slice B for the evaluator). A document can carry exceptions,
``DocumentAccessRule`` rows: ``allow`` shares just that document with someone who
cannot read the space, ``deny`` hides just that document from someone who can.
Deny wins. ``services.doc_acl`` is the one place that combines the two.

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
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from quberi_api.db.base import Base
from quberi_api.db.types import TimestampMixin, UUIDPk
from quberi_api.domain._constraints import in_values
from quberi_api.domain.enums import (
    DOCUMENT_REVIEW_STATES,
    DOCUMENT_ACCESS_EFFECTS,
    DOCUMENT_ACCESS_SUBJECT_TYPES,
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
        in_values("review_state", DOCUMENT_REVIEW_STATES, name="review_state"),
        Index("ix_documents_organization_id", "organization_id"),
        Index(
            "ix_documents_space_id_review_state",
            "space_id",
            "review_state",
            postgresql_where=text("review_state <> 'approved'"),
        ),
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
    # ---- Upload approval -------------------------------------------------
    # A space can ask for approval of what editors upload. Until a reviewer
    # approves, the document is ``pending``: nothing is indexed and only the
    # uploader and the reviewers can see it. See ``services.documents.review``.
    review_state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="approved", server_default="approved"
    )
    reviewed_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[Optional["DateTime"]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
class DocumentAccessRule(TimestampMixin, Base):
    """One document-level exception to its space's permissions.

    ``effect='allow'`` lets the subject read this document even without access to
    its space (and nothing else in that space). ``effect='deny'`` hides this
    document from the subject even when their space role would let them read it.
    Deny wins over every allow and over every space role. The subject kinds are
    the ones a space can be shared with; ``organization`` means everyone in the
    organisation and its ``subject_id`` is the organisation's own id.

    Rules are read at query time, so a change applies to the next request with
    nothing to re-index.
    """

    __tablename__ = "document_access_rules"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "effect",
            "subject_type",
            "subject_id",
            name="uq_document_access_rules_rule",
        ),
        in_values("effect", DOCUMENT_ACCESS_EFFECTS, name="effect"),
        in_values("subject_type", DOCUMENT_ACCESS_SUBJECT_TYPES, name="subject_type"),
        Index("ix_document_access_rules_document_id", "document_id"),
        Index(
            "ix_document_access_rules_subject",
            "organization_id",
            "subject_type",
            "subject_id",
            "effect",
        ),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    effect: Mapped[str] = mapped_column(String(8), nullable=False)
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
