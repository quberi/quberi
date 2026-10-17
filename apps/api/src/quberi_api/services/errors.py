"""Service-layer errors.

The API layer maps these to HTTP responses; the CLI layer prints them and
exits non-zero. Services never raise raw ``ValueError`` / ``LookupError`` so
callers can tell "user made a bad request" apart from "the code is buggy".
"""

from __future__ import annotations


class ServiceError(Exception):
    """Base class for domain/service-layer errors."""


class AlreadyExistsError(ServiceError):
    """A uniqueness constraint would be violated (e.g. slug already taken)."""


class NotFoundError(ServiceError):
    """A required entity could not be located."""


class InvalidInputError(ServiceError):
    """The caller provided data that fails validation before hitting the DB."""


class QuotaExceededError(ServiceError):
    """A plan/tier resource limit would be exceeded (e.g. max spaces)."""


class ForbiddenError(ServiceError):
    """The caller may not do this, even though they can see it (HTTP 403)."""
