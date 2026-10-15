"""The upload allow-list must accept the formats we can read, and only those.

Regression: prefixes ending in a dot (``application/vnd.openxmlformats-
officedocument.``) were never matched, so every .docx, .xlsx and .pptx upload
was refused with 415 even though the indexer reads all three.
"""

from __future__ import annotations

import pytest

from quberi_api.services.documents.core import _validate_mime
from quberi_api.services.documents.errors import UnsupportedMimeError

OOXML = "application/vnd.openxmlformats-officedocument."


@pytest.mark.parametrize(
    "mime",
    [
        OOXML + "wordprocessingml.document",
        OOXML + "spreadsheetml.sheet",
        OOXML + "presentationml.presentation",
        "application/msword",
        "application/vnd.ms-excel",
        "application/pdf",
        "image/png",
        "text/markdown",
    ],
)
def test_formats_we_can_read_are_accepted(mime):
    assert _validate_mime(mime) == mime


@pytest.mark.parametrize(
    "mime",
    [
        "video/mp4",
        "application/x-evil",
        "application/vnd.openxmlformats-officedocumentX",  # prefix without the dot
        "application/vnd.openxmlformats",                  # a shorter, different string
        "audio/mpeg",
    ],
)
def test_other_types_are_still_refused(mime):
    with pytest.raises(UnsupportedMimeError):
        _validate_mime(mime)


def test_matching_is_case_insensitive_and_trims():
    assert (
        _validate_mime("  " + (OOXML + "WordprocessingML.Document").upper() + " ")
        == (OOXML + "wordprocessingml.document")
    )
