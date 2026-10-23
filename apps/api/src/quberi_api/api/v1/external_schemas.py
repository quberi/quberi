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
    # "image_ocr" for text read out of a picture, "image_caption" for a model's description of
    # one, otherwise "text".
    source_kind: str = "text"
    # Which version of the file the passage was read from, and the file's newest version
    # (null when it has no version record). A passage whose number is lower than the
    # newest comes from a version that has since been replaced.
    version_number: int = 1
    latest_version: Optional[int] = None
    # The heading (Word, Markdown) or sheet (Excel) the passage sits in; empty when the format
    # has none. ``unit_label`` says what ``unit_start``/``unit_end`` and ``page_*`` count:
    # "text_block"/"ocr_block" pages of a PDF, "slide" slide numbers (in ``page_*``),
    # "sheet_row" rows of the sheet in ``section``, "paragraph" paragraphs of a Word file,
    # "line" lines of a text file.
    section: str = ""
    unit_start: Optional[int] = None
    unit_end: Optional[int] = None
    # Where the passage sits in the document's extracted text, so ``read_around`` can return the
    # text on either side of it. Meaningless for text read from a scan or a picture (those have
    # no text layer to read around).
    char_start: int = 0
    char_end: int = 0
    # Absolute link to an image of the page the passage is on (a PDF) or of the picture it was
    # read from, valid for ``preview_expires_in`` seconds. Null when this server has no signing
    # key or the passage has no page to show.
    page_preview_url: Optional[str] = None
    # Where this passage is from, ready to put after a point it supports: the document, the
    # place in it and the version when the file has more than one, for example
    # ``handbook.pdf, page 3, version 2 (latest)``. It says only what is known: a passage with no
    # recorded place is cited by the document alone. Empty on results made before this existed.
    citation: str = ""


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


class SearchImageResult(ImageResult):
    """A picture offered beside the passages of a text search.

    The same as :class:`ImageResult` plus the one thing only a text search can know: whether
    the text read out of this picture also matched the question. The dedicated picture search
    keeps returning plain ``ImageResult`` items, unchanged.
    """

    # The numbers (1-based, as in the passages list) of the passages that are the same file,
    # found by what it says as well as by what it shows. Empty otherwise.
    passage_numbers: list[int] = Field(default_factory=list)


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
    images: list[SearchImageResult] = Field(default_factory=list)
    preview_expires_in: int = 0


# ---------------------------------------------------------------------------
# Reading around a passage
# ---------------------------------------------------------------------------


class ReadAroundRequest(BaseModel):
    """Which passage to read around: the ids and offsets a search result gave."""

    model_config = ConfigDict(extra="forbid")

    document_id: uuid.UUID
    # The version the passage was found in; omit for the current one.
    version: Optional[int] = Field(default=None, ge=1, le=65535)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    # How much of the text to return on either side of the passage, in characters.
    before: int = Field(default=1500, ge=0, le=3000)
    after: int = Field(default=1500, ge=0, le=3000)


class ReadAroundResponse(BaseModel):
    document_id: uuid.UUID
    document_name: str
    space_name: str
    version_number: int
    latest_version: Optional[int] = None
    before: str
    passage: str
    after: str
    # Where the returned text begins and ends in the file's text, and how long that text is.
    window_start: int
    window_end: int
    text_length: int
    # True when the passage itself was cut short to keep the answer small.
    passage_truncated: bool = False


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
