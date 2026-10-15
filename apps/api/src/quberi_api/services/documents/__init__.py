"""Document service surface — see submodules for the split.

Re-exports the public read/write functions so callers can just:

    from quberi_api.services import documents
    await documents.upload_local(...)
"""

from quberi_api.services.documents.core import (
    DocumentPage,
    DocumentStats,
    DocumentView,
    delete_document,
    get_document,
    get_document_text,
    list_documents_in_space,
    open_download_stream,
    save_note,
    stats_for_space,
    upload_local,
)
from quberi_api.services.documents.errors import (
    DocumentPermissionError,
    DocumentValidationError,
    UnsupportedMimeError,
)

__all__ = [
    "delete_document",
    "get_document",
    "get_document_text",
    "list_documents_in_space",
    "open_download_stream",
    "save_note",
    "stats_for_space",
    "upload_local",
    "DocumentPage",
    "DocumentStats",
    "DocumentView",
    "DocumentPermissionError",
    "DocumentValidationError",
    "UnsupportedMimeError",
]
