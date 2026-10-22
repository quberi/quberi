"""Request/response models shared by the REST external API and the MCP tools.

Moved out of ``api/v1/external.py`` so ``services/mcp/adapters.py`` can reuse the
exact same Pydantic contracts without a circular import between the REST router
module and the shared business-logic module it calls into. Nothing here changed
shape or validation during the move — see ``external.py``'s and ``adapters.py``'s
module docstrings for why the split exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from quberi_api.services.rag import image_search

# ---------------------------------------------------------------------------
# Search (text + related pictures)
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=5, ge=1, le=10)
    # Name of one space to look in; omit to look in every space the person can read.
    space: Optional[str] = Field(default=None, max_length=200)
    # What to look in: ``both`` (default) the documents' text and, beside it, pictures that
    # might be what was asked for; ``text`` only the text; ``image`` only the pictures.
    kind: Literal["text", "image", "both"] = "both"


class Passage(BaseModel):
    document_id: uuid.UUID
    document_name: str
    space_id: uuid.UUID
    space_name: str
    chunk_index: int
    text: str
    score: float
    # Where in the file the passage is, so a person can open the original and check it.
    # Null for formats that do not record a page yet.
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    # "ocr_block" when any of the passage was recognised from a scan (it may contain
    # errors), "text_block" when it is the file's own text, "" when not recorded.
    unit_label: str = ""
    # "image_ocr" for text read out of a picture, otherwise "text".
    source_kind: str = "text"


class PendingDocument(BaseModel):
    """A document in a searched space that cannot be found yet."""

    document_name: str
    space_name: str
    # 'not_indexed' (queued), 'indexing' (being read right now), or 'awaiting_approval' (the
    # space approves uploads and this one, the caller's own, has not been approved yet).
    state: str


class ImageResult(BaseModel):
    name: str
    space_name: str
    mime_type: str
    size_bytes: int
    # 0..1 closeness; only useful for ordering and for judging a weak match.
    score: float
    # Absolute link to a small JPEG preview, valid for ``preview_expires_in`` seconds.
    # Null when this server has no signing key (the picture is still listed).
    preview_url: Optional[str] = None


class SearchResponse(BaseModel):
    organization: str
    passages: list[Passage]
    searched_spaces: int
    # Documents in the spaces just searched that are still being indexed, so a
    # tool can say "that file is still processing" instead of presenting
    # unrelated passages as the answer. Newest first, capped; the total is
    # reported separately.
    pending_documents: list[PendingDocument] = Field(default_factory=list)
    pending_total: int = 0
    # Pictures that might be what was asked for (``kind`` both or image). With ``both`` they
    # are only the few that score well enough and are *possible* matches, to be offered
    # beside the passages; with ``image`` they are simply the closest ones.
    images: list[ImageResult] = Field(default_factory=list)
    preview_expires_in: int = 0


# ---------------------------------------------------------------------------
# Picture search with previews
# ---------------------------------------------------------------------------


class ImageSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=image_search.MAX_QUERY_TEXT_CHARS)
    top_k: int = Field(default=6, ge=1, le=10)
    # Name of one space to look in; omit to look in every space the person can read.
    space: Optional[str] = Field(default=None, max_length=200)


class ImageSearchResponse(BaseModel):
    organization: str
    images: list[ImageResult]
    searched_spaces: int
    preview_expires_in: int


# ---------------------------------------------------------------------------
# Browse: spaces and documents
# ---------------------------------------------------------------------------


class SpaceInfo(BaseModel):
    name: str
    # More than 0 when the person cannot read this space as a whole and sees only that
    # many documents of it, each shared with them individually. Then ``document_count``
    # is the same number.
    shared_only_documents: int = 0
    # 'personal' (the person's own space) or 'shared'.
    kind: str
    # Whether save and upload would be allowed there (editor or above).
    can_edit: bool
    document_count: int


class SpacesResponse(BaseModel):
    organization: str
    spaces: list[SpaceInfo]
    # How many spaces the person can read in all (the list is capped).
    total: int


class DocumentInfo(BaseModel):
    name: str
    mime_type: str
    size_bytes: int
    updated_at: datetime
    # 'ready' (searchable), 'indexing' (being processed now),
    # 'awaiting_approval' / 'rejected' (the space approves uploads; this one is the caller's own
    # and has not been approved, so it is not being indexed) or
    # 'not_searchable' (processing failed or never finished, or a kind of file
    # that cannot be searched by content, such as an image).
    status: str
    # Why a reviewer rejected it. Set only on the caller's own rejected files, so the person who
    # uploaded (or the tool acting for them) can find out why it is not searchable.
    review_note: Optional[str] = None


class DocumentsResponse(BaseModel):
    space_name: str
    documents: list[DocumentInfo]
    # Documents in this space in all, so a tool can say "50 of 212".
    total: int
    # Pass back as ``cursor`` for the next page; null on the last page.
    next_cursor: Optional[str] = None
    # The kind filter that was applied, echoed so a tool can tell a server that
    # honoured it from an older one that ignored the parameter.
    type: Optional[str] = None


# ---------------------------------------------------------------------------
# Save a note
# ---------------------------------------------------------------------------


class SaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1)
    # Named in words, not an id: the tool/model refers to a space the way the
    # person does. Omit to use the person's personal space.
    space: Optional[str] = Field(default=None, max_length=200)
    source_label: Optional[str] = Field(default=None, max_length=200)
    source_url: Optional[str] = Field(default=None, max_length=2000)


class SaveResponse(BaseModel):
    document_id: uuid.UUID
    space_id: uuid.UUID
    space_name: str
    name: str
    # 'approved', or 'pending' when the space asks for approval of uploads: the note is saved
    # but not indexed (so not searchable) until an owner or admin of the space approves it.
    review_state: str = "approved"


# ---------------------------------------------------------------------------
# Upload a file
# ---------------------------------------------------------------------------


class UploadResponse(BaseModel):
    document_id: uuid.UUID
    space_id: uuid.UUID
    space_name: str
    name: str
    mime_type: str
    size_bytes: int
    # 'approved', or 'pending' when the space asks for approval of uploads (see SaveResponse).
    review_state: str = "approved"
    # True when this space already held an identical file (same SHA-256), so nothing was
    # uploaded. Then ``document_id``, ``name`` and ``review_state`` describe the file that
    # was already there. Absent (false) on a normal upload, so existing clients are unaffected.
    duplicate: bool = False
