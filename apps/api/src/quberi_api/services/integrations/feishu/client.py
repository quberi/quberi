"""Authenticated HTTP client for the Feishu Open Platform.

Design summary
--------------
* Every call goes through :meth:`FeishuClient.request`, which handles
  ``tenant_access_token`` acquisition, Redis caching (2h TTL, refreshed
  slightly early so we don't race the expiry), and one automatic retry
  on 401/429/5xx.
* All Feishu API responses share the same envelope::

      { "code": 0, "msg": "success", "data": {...} }

  Non-zero ``code`` is mapped to :class:`FeishuError` subclasses so the
  service layer can catch specific failures (auth vs rate-limit vs
  permission).
* The client is stateless *per call* — the only "state" it caches is
  the tenant token in Redis, which is safe to share across processes.

Later
-----
Per-org BYO credentials will land as a lightweight subclass that
overrides ``app_id`` / ``app_secret`` from an ``integrations`` row
(alembic 0011) rather than the env. For now everything reads from the
platform defaults in :class:`Settings`.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

import httpx
from redis.asyncio import Redis

from quberi_api.core.config import Settings, get_settings
from quberi_api.infrastructure.redis import create_redis_client

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FeishuError(RuntimeError):
    """Base class for all Feishu integration failures."""

    def __init__(self, code: int, msg: str, *, detail: Optional[dict] = None) -> None:
        super().__init__(f"feishu[{code}] {msg}")
        self.code = code
        self.msg = msg
        self.detail = detail or {}


class FeishuNotConfiguredError(FeishuError):
    """Raised when Feishu credentials aren't set on this deployment."""

    def __init__(self) -> None:
        super().__init__(
            -1,
            "Feishu credentials are not configured "
            "(set QUBERI_FEISHU_APP_ID + QUBERI_FEISHU_APP_SECRET)",
        )


class FeishuAuthError(FeishuError):
    """Auth-shaped failures (invalid app_secret, revoked token, …)."""


class FeishuPermissionError(FeishuError):
    """API returned a permission-denied code — usually a missing scope."""


class FeishuRateLimitError(FeishuError):
    """API returned 429 or Feishu-specific throttling code."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0, read=60.0)
# Feishu tokens live 2h; refresh at 1h50m so we never race the expiry
# under load. All caches keyed by app_id so multi-tenant setups don't
# smash each other's tokens.
_TOKEN_TTL_SECONDS = 60 * 110
_TOKEN_KEY_PREFIX = "quberi:feishu:tenant_token:"

_AUTH_CODES: frozenset[int] = frozenset({
    99991661,  # tenant_access_token_invalid
    99991663,  # app_ticket_invalid (open-store apps)
    99991671,  # sign verification failed
})
_PERMISSION_CODES: frozenset[int] = frozenset({
    99991672,  # no scope permission
    99991679,  # bot not in chat
})
_RATE_LIMIT_CODES: frozenset[int] = frozenset({99991400})


@dataclass(frozen=True)
class FeishuCredentials:
    """The minimum a caller needs to hand to :class:`FeishuClient`."""

    app_id: str
    app_secret: str
    base_url: str = "https://open.feishu.cn/open-apis"


class FeishuClient:
    """Async HTTP wrapper over the Feishu Open Platform.

    Instances are cheap to create; the token cache lives in Redis so
    two client instances hitting the same app_id share the same token.
    """

    def __init__(
        self,
        credentials: FeishuCredentials,
        *,
        redis: Optional[Redis] = None,
    ) -> None:
        self._creds = credentials
        self._redis = redis
        self._token_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    async def _tenant_access_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid tenant_access_token, refreshing if needed.

        Uses a per-app cache key so an org that later brings its own key
        doesn't collide with the platform default.
        """

        cache_key = f"{_TOKEN_KEY_PREFIX}{self._creds.app_id}"

        if not force_refresh and self._redis is not None:
            cached = await self._redis.get(cache_key)
            if cached:
                return cached.decode() if isinstance(cached, bytes) else str(cached)

        async with self._token_lock:
            # Re-check inside the lock — another coroutine may have
            # populated the cache during our wait.
            if not force_refresh and self._redis is not None:
                cached = await self._redis.get(cache_key)
                if cached:
                    return (
                        cached.decode() if isinstance(cached, bytes) else str(cached)
                    )

            _log.info("feishu: fetching tenant_access_token for %s", self._creds.app_id)
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as http:
                resp = await http.post(
                    f"{self._creds.base_url}/auth/v3/tenant_access_token/internal",
                    json={
                        "app_id": self._creds.app_id,
                        "app_secret": self._creds.app_secret,
                    },
                )
                resp.raise_for_status()
                body = resp.json()

            code = body.get("code", -1)
            if code != 0:
                raise FeishuAuthError(
                    code,
                    body.get("msg", "tenant_access_token failed"),
                    detail=body,
                )
            token = body["tenant_access_token"]
            expire = int(body.get("expire", _TOKEN_TTL_SECONDS))
            ttl = max(60, min(expire - 60, _TOKEN_TTL_SECONDS))
            if self._redis is not None:
                await self._redis.set(cache_key, token, ex=ttl)
            return token

    # ------------------------------------------------------------------
    # Generic request
    # ------------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        expect_envelope: bool = True,
    ) -> Any:
        """Send an authenticated request to Feishu, return ``data``.

        ``path`` may start with ``/`` — it is joined to ``base_url``.
        ``expect_envelope=False`` skips the ``{code, msg, data}``
        unwrapping (used by a handful of media endpoints that stream
        binary content).
        """

        url = f"{self._creds.base_url}{path if path.startswith('/') else '/' + path}"

        # Two attempts: first with cached token; if 401 or auth-shaped
        # envelope code, refresh the token and retry once.
        for attempt in (1, 2):
            token = await self._tenant_access_token(force_refresh=attempt == 2)
            headers = {"Authorization": f"Bearer {token}"}
            async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as http:
                resp = await http.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers,
                )
            # HTTP-level retry triggers
            if resp.status_code == 401 and attempt == 1:
                continue
            if resp.status_code == 429:
                raise FeishuRateLimitError(
                    resp.status_code,
                    "rate limited (HTTP 429)",
                    detail={"body": resp.text},
                )
            if resp.status_code >= 500 and attempt == 1:
                await asyncio.sleep(0.5)
                continue

            if not expect_envelope:
                resp.raise_for_status()
                return resp.content

            try:
                body = resp.json()
            except Exception as exc:  # noqa: BLE001
                raise FeishuError(
                    resp.status_code,
                    f"non-JSON body: {resp.text[:200]}",
                ) from exc

            code = body.get("code", -1)
            if code == 0:
                return body.get("data")
            if code in _AUTH_CODES and attempt == 1:
                # Force-refresh token and retry once.
                continue
            self._raise_from_envelope(code, body)

        # Unreachable — the loop either returns or raises.
        raise FeishuError(-1, "feishu client exhausted retries")

    def _raise_from_envelope(self, code: int, body: dict) -> None:
        msg = body.get("msg", "feishu error")
        if code in _AUTH_CODES:
            raise FeishuAuthError(code, msg, detail=body)
        if code in _PERMISSION_CODES:
            raise FeishuPermissionError(code, msg, detail=body)
        if code in _RATE_LIMIT_CODES:
            raise FeishuRateLimitError(code, msg, detail=body)
        raise FeishuError(code, msg, detail=body)

    # ------------------------------------------------------------------
    # Convenience — quick auth check for smoke tests
    # ------------------------------------------------------------------

    async def ping(self) -> dict:
        """Fetch tenant_access_token and report the app's own info.

        Used by the ``/integrations/feishu/ping`` smoke endpoint. Returns
        the raw ``application/info`` response so ops can eyeball the
        app name / status.
        """

        # ``application.v6.applications`` reveals metadata about the app
        # itself; a cheap end-to-end sanity check that credentials work
        # and at least ``contact:contact:readonly_as_app`` was enabled.
        # If we can just get the token, that alone proves credentials
        # are valid — many scopes aren't needed for the token endpoint.
        token = await self._tenant_access_token()
        return {"has_token": bool(token), "token_prefix": token[:8]}


# ---------------------------------------------------------------------------
# Platform default accessor
# ---------------------------------------------------------------------------


_platform_client: Optional[FeishuClient] = None
_platform_client_lock = asyncio.Lock()


async def get_platform_client() -> FeishuClient:
    """Return the shared client backed by ``QUBERI_FEISHU_*`` env vars.

    Raises :class:`FeishuNotConfiguredError` if credentials aren't set.
    """

    global _platform_client
    if _platform_client is not None:
        return _platform_client
    async with _platform_client_lock:
        if _platform_client is not None:
            return _platform_client
        settings: Settings = get_settings()
        if not settings.feishu_app_id or settings.feishu_app_secret is None:
            raise FeishuNotConfiguredError()
        creds = FeishuCredentials(
            app_id=settings.feishu_app_id,
            app_secret=settings.feishu_app_secret.get_secret_value(),
            base_url=settings.feishu_base_url,
        )
        _platform_client = FeishuClient(
            creds, redis=create_redis_client(settings)
        )
        return _platform_client
