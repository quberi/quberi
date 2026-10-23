"""Map service-layer errors to HTTP responses.

Each ``ServiceError`` subtype pairs with a specific HTTP status code:

* ``InvalidInputError`` → 422 (client sent something that fails validation)
* ``AlreadyExistsError`` → 409 (unique constraint would be violated)
* ``NotFoundError``      → 404
* every other ``ServiceError`` → 500 (with a generic message; the traceback
  goes to the logs but not the response)

Registration lives in ``register_exception_handlers`` so ``main.py`` stays a
one-liner.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from quberi_api.api.admin_errors import AdminApiException
from quberi_api.api.security import SessionInvalidError
from quberi_api.services.admin_messages import pick_language, render
from quberi_api.services.ai.resolver import ProviderResolutionError
from quberi_api.services.auth.account_status import AccountUnavailableError
from quberi_api.services.errors import (
    AlreadyExistsError,
    ForbiddenError,
    InvalidInputError,
    NotFoundError,
    OffboardingRequestError,
    QuotaExceededError,
    ServiceError,
)
from quberi_api.services.org_units import CycleError, HasChildrenError
from quberi_api.services.usage import FreeQuotaExhaustedError
from quberi_api.services.positions import (
    FrozenPositionError,
    HeadcountExceededError,
    OccupantsRemainError,
)

logger = logging.getLogger(__name__)


def _payload(detail: str) -> dict[str, str]:
    return {"detail": detail}


async def _handle_admin_api_exception(
    request: Request, exc: AdminApiException
) -> JSONResponse:
    # ``detail`` follows Accept-Language (English without it); ``code`` and ``params`` let
    # the platform admin console show the text from its own dictionary instead.
    content: dict[str, object] = {
        "detail": render(
            exc.code, pick_language(request.headers.get("accept-language")), exc.params
        ),
        "code": exc.code,
    }
    if exc.params:
        content["params"] = exc.params
    return JSONResponse(
        status_code=exc.status_code, content=content, headers=exc.headers
    )


async def _handle_invalid_input(_: Request, exc: InvalidInputError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=_payload(str(exc)),
    )


async def _handle_already_exists(_: Request, exc: AlreadyExistsError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=_payload(str(exc)),
    )


async def _handle_quota_exceeded(_: Request, exc: QuotaExceededError) -> JSONResponse:
    # A plan/tier limit was hit; 409 matches the "can't create more" family.
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": str(exc), "code": "quota_exceeded"},
    )


async def _handle_forbidden(_: Request, exc: ForbiddenError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={"detail": str(exc), "code": "forbidden"},
    )


async def _handle_free_quota_exhausted(
    _: Request, exc: FreeQuotaExhaustedError
) -> JSONResponse:
    # Free allowance spent. 402 Payment Required signals "add your own key or
    # top up to continue"; the client keys off ``code``.
    return JSONResponse(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        content={"detail": str(exc), "code": "free_quota_exhausted"},
    )


async def _handle_cycle(_: Request, exc: CycleError) -> JSONResponse:
    # A "would form a cycle" move is a client-side violation of the tree
    # invariant; 409 matches the ``AlreadyExists`` family semantically.
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=_payload(str(exc)),
    )


async def _handle_has_children(_: Request, exc: HasChildrenError) -> JSONResponse:
    # Archiving a parent with active children is another tree-invariant
    # violation.
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=_payload(str(exc)),
    )


async def _handle_occupants_remain(
    _: Request, exc: OccupantsRemainError
) -> JSONResponse:
    # Same shape: caller needs to detach occupants first, then retry.
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=_payload(str(exc)),
    )


async def _handle_headcount_exceeded(
    _: Request, exc: HeadcountExceededError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=_payload(str(exc)),
    )


async def _handle_frozen_position(
    _: Request, exc: FrozenPositionError
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content=_payload(str(exc)),
    )


async def _handle_not_found(_: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content=_payload(str(exc)),
    )


async def _handle_session_invalid(_: Request, exc: SessionInvalidError) -> JSONResponse:
    # ``code`` is what the web client keys off to sign the person out.
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": exc.detail, "code": "session_invalid"},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _handle_account_unavailable(
    _: Request, exc: AccountUnavailableError
) -> JSONResponse:
    # Sign-in refused (no session exists yet), so this is not "session_invalid".
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": str(exc), "code": "account_unavailable"},
    )


async def _handle_offboarding_request(
    _: Request, exc: OffboardingRequestError
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status,
        content={"detail": str(exc), "code": exc.code},
    )


async def _handle_provider_unresolved(
    _: Request, exc: ProviderResolutionError
) -> JSONResponse:
    # No model can serve this organisation right now (for example the key behind its
    # selection was removed). A fixed message: the exception text carries ids.
    logger.warning("model provider could not be resolved: %s", type(exc).__name__)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "detail": "此组织的模型尚未配置,请联系管理员。 "
            "The organisation's model is not configured. Contact an administrator.",
            "code": "model_not_configured",
        },
    )


async def _handle_service_error(_: Request, exc: ServiceError) -> JSONResponse:
    # Programmer error surfaced through a ServiceError subclass we do not have
    # a dedicated mapping for — log and hide the details.
    logger.exception("unhandled service error", exc_info=exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=_payload("internal service error"),
    )


def register_exception_handlers(app: FastAPI) -> None:
    # Order matters: FastAPI matches the *first* handler whose exception type
    # matches ``isinstance``. Register concrete subclasses before the base.
    app.add_exception_handler(AdminApiException, _handle_admin_api_exception)
    app.add_exception_handler(InvalidInputError, _handle_invalid_input)
    app.add_exception_handler(QuotaExceededError, _handle_quota_exceeded)
    app.add_exception_handler(FreeQuotaExhaustedError, _handle_free_quota_exhausted)
    app.add_exception_handler(CycleError, _handle_cycle)
    app.add_exception_handler(HasChildrenError, _handle_has_children)
    app.add_exception_handler(OccupantsRemainError, _handle_occupants_remain)
    app.add_exception_handler(HeadcountExceededError, _handle_headcount_exceeded)
    app.add_exception_handler(FrozenPositionError, _handle_frozen_position)
    app.add_exception_handler(SessionInvalidError, _handle_session_invalid)
    app.add_exception_handler(AccountUnavailableError, _handle_account_unavailable)
    app.add_exception_handler(OffboardingRequestError, _handle_offboarding_request)
    app.add_exception_handler(ProviderResolutionError, _handle_provider_unresolved)
    app.add_exception_handler(ForbiddenError, _handle_forbidden)
    app.add_exception_handler(AlreadyExistsError, _handle_already_exists)
    app.add_exception_handler(NotFoundError, _handle_not_found)
    app.add_exception_handler(ServiceError, _handle_service_error)
