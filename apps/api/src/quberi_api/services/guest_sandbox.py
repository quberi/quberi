"""The guest sandbox: a shared space where an anonymous visitor can try uploading.

A visitor can upload a file, ask questions about it, and delete it - all
without an account. Everything here is built around one fact: the space is
public. Anyone can see the file list, anything uploaded may be abusive, and
the visitor is identified by nothing more than a random value their browser
made up. So:

* **Ownership is a browser token, stored only as a hash.** It decides which
  files a visitor may delete and which ones their questions search. It is not
  authentication and is not meant to withstand a determined attacker.
* **Uploads are small, few and short-lived** (size / type / count limits, and
  they expire after :data:`TTL_HOURS`), because storing and embedding them
  costs real money and the area will attract junk.
* **Deletion is verified.** The shared document-delete path swallows failures
  when removing vectors; here a leftover vector would mean a deleted file can
  still be quoted back to someone, so after deleting we count what remains and
  retry.
* **The product guide never reads this space** unless it is asked to search a
  visitor's own files, and then it treats them as untrusted (see
  ``demo_content``): this is the part that keeps a hostile upload from rewriting
  what Quberi says about itself.
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from qdrant_client import AsyncQdrantClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.documents.models import Document
from quberi_api.domain.spaces.models import Space
from quberi_api.services.rag.vector_store import VectorStore

_log = logging.getLogger(__name__)

SANDBOX_SLUG = "guest-sandbox"

# How long an upload lives. Chosen by the product owner.
TTL_HOURS = 24

# Per-file and per-area limits. Small on purpose: every file is embedded on the
# platform's own hardware, and the area is anonymous.
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_ACTIVE_DOCUMENTS = 200  # whole sandbox; new uploads are refused beyond this
MAX_FILES_PER_BROWSER = 5  # at one time, per browser token

# Plain-text-like and common office formats only. The server decides by file
# extension *and* content type, because a content type alone is client-supplied.
ALLOWED_EXTENSIONS = (".txt", ".md", ".markdown", ".csv", ".pdf", ".docx")
ALLOWED_CONTENT_TYPES = (
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/x-markdown",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/octet-stream",  # browsers often send this for .md; extension still gates
)

# A token is whatever a browser generated: any reasonably long URL-safe string.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{22,128}$")


class SandboxError(Exception):
    """A sandbox rule was violated. ``code`` is stable for the client."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# Identity: the browser token
# ---------------------------------------------------------------------------


def valid_token(token: Optional[str]) -> bool:
    return bool(token) and _TOKEN_RE.match(token or "") is not None


def hash_token(token: str) -> str:
    """The only form of the token that is ever stored."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def owner_hash_of(doc_attributes: Optional[dict]) -> Optional[str]:
    value = (doc_attributes or {}).get("guest_owner")
    return value if isinstance(value, str) else None


def is_owned_by(doc_attributes: Optional[dict], token: Optional[str]) -> bool:
    """True iff the document was uploaded with this browser's token."""

    if not valid_token(token):
        return False
    mine = owner_hash_of(doc_attributes)
    return mine is not None and mine == hash_token(token)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Upload rules
# ---------------------------------------------------------------------------


def check_upload(*, filename: str, content_type: str, size: int) -> None:
    """Raise :class:`SandboxError` unless the file may be uploaded."""

    name = (filename or "").strip().lower()
    if not name.endswith(ALLOWED_EXTENSIONS):
        raise SandboxError(
            "unsupported_type",
            "游客只能上传 txt / md / csv / pdf / docx 文件。 "
            "Guests can only upload txt, md, csv, pdf or docx files.",
        )
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype and ctype not in ALLOWED_CONTENT_TYPES:
        raise SandboxError(
            "unsupported_type",
            "这个文件类型不受支持。 This file type is not supported.",
        )
    if size <= 0:
        raise SandboxError("empty", "文件是空的。 The file is empty.")
    if size > MAX_FILE_BYTES:
        raise SandboxError(
            "too_large",
            f"游客上传的单个文件不能超过 {MAX_FILE_BYTES // (1024 * 1024)} MB。 "
            f"Guest uploads are limited to {MAX_FILE_BYTES // (1024 * 1024)} MB per file.",
        )


def expiry_attributes(token: str, *, now: Optional[datetime] = None) -> dict:
    """Attributes stored on a sandbox document: who owns it, and when it dies."""

    now = now or datetime.now(timezone.utc)
    return {
        "guest_owner": hash_token(token),
        "guest_expires_at": (now + timedelta(hours=TTL_HOURS)).isoformat(),
    }


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


async def get_sandbox_space(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> Optional[Space]:
    return (
        await session.execute(
            select(Space).where(
                Space.organization_id == organization_id, Space.slug == SANDBOX_SLUG
            )
        )
    ).scalar_one_or_none()


async def count_active(session: AsyncSession, *, space_id: uuid.UUID) -> int:
    return int(
        (
            await session.execute(
                select(func.count()).select_from(Document).where(Document.space_id == space_id)
            )
        ).scalar_one()
    )


async def owned_documents(
    session: AsyncSession, *, space_id: uuid.UUID, token: str
) -> list[Document]:
    """Every sandbox document uploaded with ``token`` (compared by hash)."""

    if not valid_token(token):
        return []
    mine = hash_token(token)
    rows = (
        (await session.execute(select(Document).where(Document.space_id == space_id)))
        .scalars()
        .all()
    )
    return [d for d in rows if owner_hash_of(d.attributes) == mine]


def is_expired(doc_attributes: Optional[dict], *, now: Optional[datetime] = None) -> bool:
    raw = (doc_attributes or {}).get("guest_expires_at")
    if not isinstance(raw, str):
        return False
    try:
        expires = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) >= expires


# ---------------------------------------------------------------------------
# Verified deletion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PurgeResult:
    document_id: uuid.UUID
    vectors_left: int  # 0 = verified clean


async def purge_vectors(
    *,
    qdrant: AsyncQdrantClient,
    document_id: uuid.UUID,
    model_id: str,
    vector_dim: int,
    attempts: int = 3,
) -> PurgeResult:
    """Delete a document's vectors and *check* they are gone, retrying.

    The shared delete path swallows vector-store errors, which would leave a
    deleted file's text searchable. This calls the store directly, counts what
    remains, and retries; the caller decides what to do about a non-zero result.
    """

    store = VectorStore(qdrant, model_id=model_id, vector_dim=vector_dim)
    left = -1
    for attempt in range(1, attempts + 1):
        try:
            await store.delete_document(document_id=document_id)
            left = await store.count_document(document_id=document_id)
        except Exception:  # noqa: BLE001
            _log.exception(
                "sandbox: vector purge attempt %d/%d failed for %s",
                attempt,
                attempts,
                document_id,
            )
            continue
        if left == 0:
            break
    return PurgeResult(document_id=document_id, vectors_left=max(left, 0) if left >= 0 else 1)
