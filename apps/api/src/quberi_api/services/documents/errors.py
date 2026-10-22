"""Document-specific service errors."""

from quberi_api.services.errors import InvalidInputError, ServiceError


class DocumentValidationError(InvalidInputError):
    """Filename / size / mime failed validation."""


class UnsupportedMimeError(DocumentValidationError):
    """MIME type is not on the allow-list."""


class DuplicateDocumentError(ServiceError):
    """An identical file (same SHA-256) is already in this space.

    Only a file that is waiting for approval or approved counts; a rejected copy
    does not block a new upload. ``document_id`` / ``name`` / ``review_state``
    describe the existing file and are set only when ``readable`` is true: a rule
    may hide that file from the person uploading, and then saying which file it is
    would tell them it exists. The HTTP layer maps this to 409; outside tools get
    a "not uploaded, already there" answer when the file is readable.
    """

    def __init__(
        self,
        message: str,
        *,
        readable: bool,
        document_id=None,
        name: str | None = None,
        review_state: str | None = None,
    ) -> None:
        super().__init__(message)
        self.readable = readable
        self.document_id = document_id
        self.name = name
        self.review_state = review_state


class VersionConflictError(ServiceError):
    """A new version cannot be added right now: the file is the one the document already
    has, or another version was added at the same moment. The HTTP layer maps this to 409.
    """


class DocumentPermissionError(ServiceError):
    """Caller lacks the required role on the parent space.

    The HTTP layer maps this to 403 (or 404 when we prefer to hide
    existence — see ``core.list_documents_in_space``).
    """
