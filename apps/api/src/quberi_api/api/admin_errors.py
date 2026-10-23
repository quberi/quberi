"""Errors on the platform admin API that carry a stable message code.

``AdminApiException`` is an ``HTTPException`` whose ``detail`` is the English text, so an
app without the handler in ``api.errors`` still answers ``{"detail": "<english>"}`` exactly
as before. With the handler registered, the body also carries ``code`` and ``params`` and
``detail`` follows the request's ``Accept-Language``.

This module imports only FastAPI and the catalog so ``api.security`` can use it without a
cycle through ``api.errors``.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException

from quberi_api.services.admin_messages import CodedValueError, render


class AdminApiException(HTTPException):
    def __init__(
        self,
        status_code: int,
        code: str,
        *,
        headers: Optional[dict[str, str]] = None,
        **params: Any,
    ) -> None:
        super().__init__(
            status_code=status_code, detail=render(code, "en", params), headers=headers
        )
        self.code = code
        self.params = params


def from_coded(exc: CodedValueError, status_code: int) -> AdminApiException:
    """The API error for a service-layer :class:`CodedValueError`."""

    return AdminApiException(status_code, exc.code, **exc.params)
