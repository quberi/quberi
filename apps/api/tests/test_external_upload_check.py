"""What an outside tool may upload: the content decides, not the label.

These are plain-function tests of ``external_upload.check_upload`` (no database).
They pin the rules that keep a model-driven tool from putting dangerous or
mislabelled files into a knowledge base:

* a short allow-list; archives, HTML, SVG and "anything" (octet-stream) refused;
* the bytes are inspected, so a lying type or a lying extension is caught;
* Office files are zip containers but are accepted only when they really are
  that Office format - never as a generic zip.
"""

from __future__ import annotations

import io
import zipfile

import pytest

from quberi_api.services.documents.errors import (
    DocumentValidationError,
    UnsupportedMimeError,
)
from quberi_api.services.documents.external_upload import check_upload

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 32
PDF = b"%PDF-1.7\n" + b"x" * 32

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


def _zip(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _office(main: str) -> bytes:
    return _zip({"[Content_Types].xml": b"<Types/>", main: b"<x/>"})


def _check(data: bytes, name: str, declared: str = ""):
    return check_upload(data=data, filename=name, declared_mime=declared)


# --------------------------------------------------------------------------
# What is accepted, and what we store it as
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data,name,mime",
    [
        (PNG, "a.png", "image/png"),
        (JPEG, "a.jpg", "image/jpeg"),
        (JPEG, "a.jpeg", "image/jpeg"),
        (GIF, "a.gif", "image/gif"),
        (WEBP, "a.webp", "image/webp"),
        (PDF, "a.pdf", "application/pdf"),
        (_office("word/document.xml"), "a.docx", DOCX_MIME),
        (_office("xl/workbook.xml"), "a.xlsx", XLSX_MIME),
        (_office("ppt/presentation.xml"), "a.pptx", PPTX_MIME),
        (b"# Title\n\nbody\n", "notes.md", "text/markdown"),
        (b"a,b\n1,2\n", "t.csv", "text/csv"),
        (b"plain words\n", "t.txt", "text/plain"),
        (b'{"a": 1}', "d.json", "application/json"),
    ],
)
def test_allowed_files_are_detected_by_their_content(data, name, mime):
    got = _check(data, name)
    assert got.mime == mime
    assert got.filename == name


def test_chinese_text_is_fine():
    assert _check("你好，世界。\n".encode(), "说明.txt").mime == "text/plain"


# --------------------------------------------------------------------------
# The bytes decide, not the label
# --------------------------------------------------------------------------


def test_the_stored_type_is_the_detected_one_not_the_claimed_one():
    # A generic "octet-stream" claim is allowed, but the stored type is the real one.
    assert _check(PNG, "a.png", "application/octet-stream").mime == "image/png"


@pytest.mark.parametrize(
    "data,name,declared",
    [
        (PDF, "a.png", "image/png"),            # says PNG, is a PDF
        (PNG, "a.pdf", "application/pdf"),      # says PDF, is a PNG
        (JPEG, "a.png", "image/png"),
        (b"plain text", "a.txt", "image/png"),  # text pretending to be an image
    ],
)
def test_a_declared_type_that_contradicts_the_bytes_is_refused(data, name, declared):
    with pytest.raises((DocumentValidationError, UnsupportedMimeError)):
        _check(data, name, declared)


def test_a_misleading_extension_is_corrected_so_html_cannot_hide_a_picture():
    got = _check(PNG, "report.html")
    assert got.mime == "image/png"
    assert got.filename == "report.html.png"
    assert not got.filename.lower().endswith(".html")


def test_a_missing_extension_gets_the_right_one():
    assert _check(PDF, "report").filename == "report.pdf"
    assert _check(JPEG, "photo").filename == "photo.jpg"


def test_jpg_alias_declarations_are_tolerated():
    assert _check(JPEG, "a.jpg", "image/jpg").mime == "image/jpeg"


# --------------------------------------------------------------------------
# Refused: archives, web content, unknown binaries
# --------------------------------------------------------------------------


def test_a_plain_zip_is_refused():
    plain = _zip({"a.txt": b"hello"})
    with pytest.raises(UnsupportedMimeError):
        _check(plain, "a.zip", "application/zip")
    with pytest.raises(UnsupportedMimeError):
        _check(plain, "a.docx", DOCX_MIME)  # renaming it does not help


def test_a_zip_with_office_names_but_no_content_types_is_refused():
    fake = _zip({"word/document.xml": b"<x/>"})  # no [Content_Types].xml
    with pytest.raises(UnsupportedMimeError):
        _check(fake, "a.docx", DOCX_MIME)


def test_a_zip_with_content_types_but_no_known_main_part_is_refused():
    odd = _zip({"[Content_Types].xml": b"<Types/>", "payload.exe": b"MZ"})
    with pytest.raises(UnsupportedMimeError):
        _check(odd, "a.docx", DOCX_MIME)


def test_a_zip_with_an_absurd_number_of_entries_is_refused():
    entries = {"[Content_Types].xml": b"<Types/>", "word/document.xml": b"<x/>"}
    entries.update({f"f{i}": b"" for i in range(6000)})
    many = _zip(entries)
    with pytest.raises(UnsupportedMimeError):
        _check(many, "a.docx", DOCX_MIME)


def test_a_zip_that_expands_to_an_absurd_size_is_refused():
    big = _zip(
        {
            "[Content_Types].xml": b"<Types/>",
            "word/document.xml": b"<x/>",
            "pad.bin": b"\x00" * (501 * 1024 * 1024),
        }
    )
    with pytest.raises(UnsupportedMimeError):
        _check(big, "a.docx", DOCX_MIME)


def test_a_corrupt_zip_is_a_clean_error_not_a_crash():
    with pytest.raises(DocumentValidationError):
        _check(b"PK\x03\x04garbage-not-a-real-zip", "a.docx", DOCX_MIME)


@pytest.mark.parametrize(
    "data,name",
    [
        (b"\x1f\x8b\x08\x00" + b"\x00" * 20, "a.tar.gz"),          # gzip
        (b"ustar" + b"\x00" * 600, "a.tar"),                       # tar-ish
        (b"MZ\x90\x00" + b"\x00" * 40, "a.exe"),                   # Windows executable
        (b"\x7fELF" + b"\x00" * 40, "a.bin"),                      # Linux executable
        (b"<html><script>alert(1)</script></html>", "a.html"),     # web page
        (b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>", "a.svg"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 40, "a.doc"),  # legacy Office
    ],
)
def test_archives_executables_and_web_content_are_refused(data, name):
    with pytest.raises(UnsupportedMimeError):
        _check(data, name)


def test_text_must_be_named_as_text():
    # No signature, no text extension: we will not guess.
    with pytest.raises(UnsupportedMimeError):
        _check(b"just some words", "mystery")
    with pytest.raises(UnsupportedMimeError):
        _check(b"just some words", "a.html")


def test_text_with_binary_bytes_is_refused():
    with pytest.raises(DocumentValidationError):
        _check(b"hello\x00world", "a.txt")
    with pytest.raises(DocumentValidationError):
        _check(b"\xff\xfe\xfa not utf-8", "a.md")


def test_an_empty_file_is_refused():
    with pytest.raises(DocumentValidationError):
        _check(b"", "a.txt")
