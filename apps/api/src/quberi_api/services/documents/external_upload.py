"""What an outside tool may upload, and how we check it really is that.

The web app lets a signed-in person pick any file their browser sends. An
outside tool (a plugin acting for that person) is a different trust level: it is
driven by a model, which can be steered by whatever text it read last. So this
path is stricter than the web one, on purpose:

* **A short allow-list**, not the web app's broad one. No archives (zip, tar,
  gzip), no ``application/octet-stream``, no HTML or SVG (both can carry script
  and are shown by browsers), no legacy binary Office formats.
* **The bytes decide, not the label.** The type the client declares and the
  filename are never trusted: we look at the content itself. A file that says it
  is a PNG but is a PDF is refused, and the stored type is the one we detected.
* **The stored name carries the right extension**, so a file cannot be saved as
  ``report.html`` while being a picture.

Office files (docx/xlsx/pptx) are zip containers underneath. They are accepted
only when the container really is the Office format it claims (the right content
types entry and the main part for that format), never as a generic zip.
"""

from __future__ import annotations

import io
import os
import zipfile
from dataclasses import dataclass
from typing import Optional

from quberi_api.services.documents.errors import (
    DocumentValidationError,
    UnsupportedMimeError,
)

# Enough bytes to recognise every format below.
SNIFF_BYTES = 16

_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# detected mime -> (canonical extension, extensions we accept for it)
_FORMATS: dict[str, tuple[str, frozenset[str]]] = {
    "image/png": (".png", frozenset({".png"})),
    "image/jpeg": (".jpg", frozenset({".jpg", ".jpeg"})),
    "image/gif": (".gif", frozenset({".gif"})),
    "image/webp": (".webp", frozenset({".webp"})),
    "application/pdf": (".pdf", frozenset({".pdf"})),
    _DOCX: (".docx", frozenset({".docx"})),
    _XLSX: (".xlsx", frozenset({".xlsx"})),
    _PPTX: (".pptx", frozenset({".pptx"})),
    "text/markdown": (".md", frozenset({".md", ".markdown"})),
    "text/csv": (".csv", frozenset({".csv"})),
    "text/plain": (".txt", frozenset({".txt", ".log"})),
    "application/json": (".json", frozenset({".json"})),
}

# What the Office container must contain (main part) per format.
_OOXML_MAIN_PART = {
    _DOCX: "word/document.xml",
    _XLSX: "xl/workbook.xml",
    _PPTX: "ppt/presentation.xml",
}

# Zip bomb guard for the Office check: we only read the central directory, but
# refuse containers that claim an absurd number of entries or expanded size.
_MAX_ZIP_ENTRIES = 5_000
_MAX_ZIP_UNCOMPRESSED = 500 * 1024 * 1024

# Kinds of text we accept, decided from the declared/extension hint, because a
# text file has no signature. The bytes must still be clean UTF-8.
_TEXT_BY_EXT = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".txt": "text/plain",
    ".log": "text/plain",
}


@dataclass(frozen=True)
class CheckedUpload:
    """A file that passed: the type we detected and the name we will store."""

    mime: str
    filename: str


def _ext(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def _looks_like_utf8_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _sniff_binary(head: bytes) -> Optional[str]:
    """Recognise the binary formats by their magic bytes."""

    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04"):
        return "zip"  # decided below: Office document or refused archive
    return None


def _classify_zip(data: bytes) -> str:
    """Which Office format this zip really is. Anything else is an archive: refused."""

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) > _MAX_ZIP_ENTRIES:
                raise UnsupportedMimeError("archive files are not accepted from outside tools")
            if sum(i.file_size for i in infos) > _MAX_ZIP_UNCOMPRESSED:
                raise UnsupportedMimeError("archive files are not accepted from outside tools")
            names = {i.filename for i in infos}
            if "[Content_Types].xml" not in names:
                raise UnsupportedMimeError("archive files are not accepted from outside tools")
            for mime, main in _OOXML_MAIN_PART.items():
                if main in names:
                    return mime
    except zipfile.BadZipFile as exc:
        raise DocumentValidationError("the file is not a valid document") from exc
    raise UnsupportedMimeError("archive files are not accepted from outside tools")


def check_upload(*, data: bytes, filename: str, declared_mime: str) -> CheckedUpload:
    """Decide what ``data`` really is and whether an outside tool may upload it.

    Raises :class:`UnsupportedMimeError` (415) for a type that is not allowed,
    :class:`DocumentValidationError` (400) for a file that is not what it says.
    """

    if not data:
        raise DocumentValidationError("the file is empty")

    kind = _sniff_binary(data[:SNIFF_BYTES])
    ext = _ext(filename)

    if kind == "zip":
        mime = _classify_zip(data)
    elif kind is not None:
        mime = kind
    else:
        # No signature: only plain text kinds are acceptable, and only if the
        # name says so AND the bytes are clean UTF-8 text.
        hinted = _TEXT_BY_EXT.get(ext)
        if hinted is None:
            raise UnsupportedMimeError(
                "this file type is not accepted from outside tools "
                "(allowed: png, jpeg, gif, webp, pdf, docx, xlsx, pptx, md, csv, txt, json)"
            )
        if not _looks_like_utf8_text(data):
            raise DocumentValidationError("the file is not valid UTF-8 text")
        mime = hinted

    # The client's own claim, if it contradicts the bytes, is a red flag. We do
    # not store its label either way, but we refuse the mismatch so a tool that
    # is confused or being steered finds out instead of silently succeeding.
    declared = (declared_mime or "").split(";")[0].strip().lower()
    generic = ("application/octet-stream", "binary/octet-stream")
    if declared and declared not in generic and not _declared_matches(declared, mime):
        raise DocumentValidationError(
            f"the file content is {mime}, which does not match the declared type {declared}"
        )

    canonical, accepted = _FORMATS[mime]
    stored_name = filename if ext in accepted else f"{filename}{canonical}"
    return CheckedUpload(mime=mime, filename=stored_name)


def _declared_matches(declared: str, detected: str) -> bool:
    if declared == detected:
        return True
    # Common, harmless aliases.
    aliases = {
        "image/jpg": "image/jpeg",
        "image/pjpeg": "image/jpeg",
        "text/x-markdown": "text/markdown",
        "application/csv": "text/csv",
    }
    if aliases.get(declared) == detected:
        return True
    # Text kinds are told apart only by name, so any text/* or json label is
    # fine for a text file as long as the bytes are text.
    text_like = {"text/plain", "text/markdown", "text/csv", "application/json"}
    return detected in text_like and (declared.startswith("text/") or declared in text_like)
