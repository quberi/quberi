import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from quberi_api.services.auth.session import read_session

from quberi_api.api.errors import register_exception_handlers
from quberi_api.api.router import router as api_router
from quberi_api.api.v1.mcp.router import mcp_router
from quberi_api.api.v1.oauth import well_known_router
from quberi_api.core.config import get_settings
from quberi_api.core.logging import configure_logging
from quberi_api.health.router import router as health_router
from quberi_api.infrastructure.registry import Infrastructure
from quberi_api.services import ai_providers, demo_seed, platform_admin
from quberi_api.services.rag.image_warmup import warm_picture_search

_log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    infrastructure = Infrastructure.create(settings)
    app.state.settings = settings
    app.state.infrastructure = infrastructure

    # Materialise (or reconcile) platform-default AI provider bindings
    # from env vars so every org has a working assistant even without
    # bringing their own key. Bootstrap is idempotent; a failure here
    # must not stop the API — orgs that BYO keys are unaffected.
    try:
        async with infrastructure.session_factory() as session:
            await ai_providers.bootstrap_platform_defaults(
                session, settings=settings
            )
    except Exception:  # pragma: no cover — logged; boot proceeds
        _log.exception("failed to bootstrap platform AI defaults")

    # Create the first platform admin account if there is none, so the operator
    # can sign in to /admin. Idempotent; a failure must not stop boot (the
    # QUBERI_PLATFORM_ADMIN_TOKEN path keeps working).
    try:
        async with infrastructure.session_factory() as session:
            await platform_admin.ensure_bootstrap_admin(session, settings=settings)
    except Exception:  # pragma: no cover — logged; boot proceeds
        _log.exception("failed to bootstrap the platform admin account")

    # Materialise the demo/sample org so unauthenticated guests have
    # something to browse. Idempotent; a failure must not stop boot.
    try:
        async with infrastructure.session_factory() as session:
            await demo_seed.ensure_demo_org(session)
    except Exception:  # pragma: no cover — logged; boot proceeds
        _log.exception("failed to seed demo organisation")
    # Bring the demo space's documents (what guests can ask about) in line
    # with ``services/demo_content.py``: stored, and queued for indexing.
    try:
        async with infrastructure.session_factory() as session:
            await demo_seed.sync_demo_content(session)
    except Exception:  # pragma: no cover — logged; boot proceeds
        _log.exception("failed to sync demo content")

    # Load the picture model's text half in the background, but only where there are
    # pictures to search: see ``services/rag/image_warmup``.
    warmup = asyncio.create_task(warm_picture_search(infrastructure.session_factory, settings))
    yield
    warmup.cancel()
    await infrastructure.close()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.environment)

    app = FastAPI(
        title="Quberi API",
        version=settings.app_version,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "Content-Type",
            "Authorization",
            "X-Request-ID",
            "X-Quberi-Organization",
            "X-Quberi-User-Email",
            # Identifies a guest's browser (hash only is stored): see
            # ``services/guest_sandbox``.
            "X-Guest-Token",
        ],
    )
    _install_guest_readonly_guard(app)
    register_exception_handlers(app)
    app.include_router(health_router)
    app.include_router(well_known_router)
    app.include_router(api_router)
    # Mounted at the application root, not under /api/v1: production puts this
    # behind the independent mcp.quberi.com domain, proxied straight into this
    # container with no path prefix (design.md §2.2/§2.11). The guest
    # read-only middleware's path check below only matches /api/v1/*, so it
    # does not apply here — the MCP endpoint's own auth (require_mcp_caller)
    # is the only gate on it, which is correct: a guest session token is a web
    # session JWT, not an OAuth access token, so it was never going to satisfy
    # require_mcp_caller's bearer-token check anyway.
    app.include_router(mcp_router)
    return app


_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _install_guest_readonly_guard(app: FastAPI) -> None:
    """Block mutating requests carried by a guest session token.

    Guests browse the demo org read-only. Rather than sprinkle a
    ``require_not_guest`` dependency across every write endpoint, we gate
    centrally here: any mutating method under ``/api/v1`` (except the auth
    routes, which guests use to *upgrade* into a real account) is rejected
    with 403 when the bearer token is a guest session.
    """

    @app.middleware("http")
    async def _guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method in _MUTATING_METHODS:
            path = request.url.path
            # ``/auth/*`` is how a guest upgrades to a real account; ``/guest/*``
            # is the demo assistant, which is stateless (it writes nothing) and
            # does its own per-IP / global rate limiting.
            if (
                path.startswith("/api/v1/")
                and not path.startswith("/api/v1/auth/")
                and not path.startswith("/api/v1/guest/")
            ):
                auth = request.headers.get("Authorization", "")
                if auth.lower().startswith("bearer "):
                    claims = read_session(auth[7:].strip())
                    if claims is not None and claims.is_guest:
                        return JSONResponse(
                            status_code=403,
                            content={
                                "detail": "登录后可用 / Sign in to do that",
                                "code": "guest_read_only",
                            },
                        )
        return await call_next(request)
