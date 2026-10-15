"""Document-specific service errors."""

from quberi_api.services.errors import InvalidInputError, ServiceError


class DocumentValidationError(InvalidInputError):
    """Filename / size / mime failed validation."""


class UnsupportedMimeError(DocumentValidationError):
    """MIME type is not on the allow-list."""


class DocumentPermissionError(ServiceError):
    """Caller lacks the required role on the parent space.

    The HTTP layer maps this to 403 (or 404 when we prefer to hide
    existence — see ``core.list_documents_in_space``).
    """
