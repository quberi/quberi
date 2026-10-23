"""AI provider CRUD + platform-default bootstrap.

Two responsibilities:

1. **CRUD** for :class:`AiProviderBinding` / :class:`AiModelSelection`,
   used by the settings UI.
2. **Bootstrap** — on API boot, materialise a platform-default binding
   from ``QUBERI_DEFAULT_DEEPSEEK_KEY`` so every org has a working
   fallback the moment the process is up. Local embedding (BGE-M3) is
   registered as a second platform default because it has no credential
   at all.

Bootstrap is idempotent — it computes the "desired platform default"
from env vars and reconciles on every boot, so rotating a key just
requires restarting the API.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.core.config import Settings
from quberi_api.domain.ai.models import AiModelSelection, AiProviderBinding
from quberi_api.services.admin_messages import CodedValueError
from quberi_api.services.ai.registry import MODEL_REGISTRY, models_for_vendor
from quberi_api.services.security.crypto import seal

_log = logging.getLogger(__name__)

_KEEP = object()  # sentinel: "field not provided, leave unchanged"


# ---------------------------------------------------------------------------
# Value types (returned to the API layer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderBindingView:
    id: uuid.UUID
    organization_id: Optional[uuid.UUID]
    identity_id: Optional[uuid.UUID]
    provider_kind: str
    vendor: Optional[str]
    display_name: str
    base_url: Optional[str]
    api_key_present: bool
    status: str
    is_platform_default: bool
    priority: int
    extra_config: dict
    models: list  # enabled model ids this binding serves (chat)
    created_at: datetime
    updated_at: datetime


def _models_of(row: AiProviderBinding) -> list:
    raw = (row.extra_config or {}).get("models")
    if isinstance(raw, list):
        return [str(m) for m in raw if m]
    return []


def _to_view(row: AiProviderBinding) -> ProviderBindingView:
    return ProviderBindingView(
        id=row.id,
        organization_id=row.organization_id,
        identity_id=row.identity_id,
        provider_kind=row.provider_kind,
        vendor=row.vendor,
        display_name=row.display_name,
        base_url=row.base_url,
        api_key_present=row.api_key_encrypted is not None,
        status=row.status,
        is_platform_default=row.is_platform_default,
        priority=row.priority,
        extra_config=row.extra_config or {},
        models=_models_of(row),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ---------------------------------------------------------------------------
# CRUD — org-scoped
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UsableChatModel:
    model_id: str
    display_name: str
    binding_id: uuid.UUID
    binding_name: str
    is_platform_default: bool
    # Which company serves this model (``deepseek``, ``openai``, ...) —
    # ``None`` for an org-owned/private binding whose vendor wasn't set.
    # Lets the UI group/label platform models by vendor instead of by
    # the raw binding display name.
    vendor: Optional[str] = None
    # True for exactly the one platform model that
    # ``QUBERI_DEFAULT_CHAT_MODEL`` names — disambiguates "the" platform
    # default now that several platform models can coexist (previously
    # every platform-sourced entry was ambiguously "the" default via
    # ``is_platform_default`` alone).
    is_default: bool = False
    # True when this entry comes from the caller's own private (BYOK)
    # binding — never present in the org-wide catalog, only in the
    # ``for_me`` picker for the user who owns it.
    is_private: bool = False


async def _platform_model_visibility_for_org(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> tuple[str, Optional[set[str]]]:
    """Return ``(mode, allowed_model_ids)`` for this org's platform models.

    ``mode`` is ``all`` / ``none`` / ``allowlist``. ``allowed_model_ids``
    is only meaningful (non-``None``) when ``mode == 'allowlist'``.
    """

    from quberi_api.domain.identity.models import Organization

    mode = (
        await session.execute(
            select(Organization.platform_model_visibility).where(
                Organization.id == organization_id
            )
        )
    ).scalar_one_or_none() or "all"

    if mode != "allowlist":
        return mode, None

    from quberi_api.domain.ai.models import OrganizationPlatformModelAllowlist

    rows = (
        await session.execute(
            select(OrganizationPlatformModelAllowlist.model_id).where(
                OrganizationPlatformModelAllowlist.organization_id == organization_id
            )
        )
    ).scalars().all()
    return mode, {str(r) for r in rows}


async def list_usable_chat_models(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> list[UsableChatModel]:
    """Flat list of chat models the org can pick — each implies its provider.

    Derived from every chat-capable binding visible to the org: its own
    org-owned bindings (always shown, never gated by platform
    visibility), plus platform defaults (gated by
    ``platform_model_visibility`` — see
    :func:`_platform_model_visibility_for_org`).

    A vendor may have *several* platform-default bindings (e.g. two
    DeepSeek keys for headroom) — they all serve the same model catalog,
    so only the one that would actually be picked at call time (lowest
    ``priority`` among ``active`` bindings; see
    :func:`services.ai.resolver._pick_platform_binding`) is used to
    source the model list, to avoid listing the same model twice.
    """

    import os

    views = await list_bindings_for_org(session, organization_id=organization_id)
    visibility_mode, allowed_model_ids = await _platform_model_visibility_for_org(
        session, organization_id=organization_id
    )
    default_chat_model_id = os.getenv("QUBERI_DEFAULT_CHAT_MODEL")

    org_views = [v for v in views if not v.is_platform_default]
    platform_views = [v for v in views if v.is_platform_default]

    # Group platform bindings by priority to pick the best for each model.
    # If multiple bindings serve the same model, the one with lowest priority wins.
    active_platform_bindings = [v for v in platform_views if v.status == "active"]
    active_platform_bindings.sort(key=lambda v: (v.priority, v.created_at))

    out: list[UsableChatModel] = []

    # Add org-owned models
    for v in org_views:
        if v.provider_kind != "openai_compat":
            continue  # only openai_compat serves chat today
        for mid in v.models:
            spec = MODEL_REGISTRY.get(mid)
            out.append(
                UsableChatModel(
                    model_id=mid,
                    display_name=spec.display_name if spec else mid,
                    binding_id=v.id,
                    binding_name=v.display_name,
                    is_platform_default=False,
                    vendor=spec.vendor if spec else v.vendor,
                )
            )

    # Add platform models (respecting visibility settings)
    if visibility_mode != "none":
        seen_models = set()  # Avoid duplicates if multiple bindings serve same model
        
        for v in active_platform_bindings:
            if v.provider_kind != "openai_compat":
                continue  # only openai_compat serves chat today
            
            model_ids = list(v.models)
            if not model_ids and default_chat_model_id:
                model_ids = [default_chat_model_id]
            
            for mid in model_ids:
                # Skip if already added by a higher-priority binding
                if mid in seen_models:
                    continue
                
                # Check visibility allowlist
                if visibility_mode == "allowlist" and (
                    allowed_model_ids is None or mid not in allowed_model_ids
                ):
                    continue
                
                spec = MODEL_REGISTRY.get(mid)
                out.append(
                    UsableChatModel(
                        model_id=mid,
                        display_name=spec.display_name if spec else mid,
                        binding_id=v.id,
                        binding_name=v.display_name,
                        is_platform_default=True,
                        vendor=spec.vendor if spec else v.vendor,
                        is_default=(mid == default_chat_model_id),
                    )
                )
                seen_models.add(mid)

    return out


async def list_usable_chat_models_for_user(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    identity_id: uuid.UUID,
) -> list[UsableChatModel]:
    """Chat models a specific user may pick: the org-wide catalog (filtered
    by access governance) **plus** that person's own private BYOK models
    (added from *any* org or their personal account — private bindings
    follow the global ``identity_id``, not this org-scoped ``user_id``).

    Governance policies only ever apply to org-shared/platform bindings —
    a private binding is the user's own choice, never gated by admin
    policy, so it is appended after filtering rather than passed through
    :func:`model_access.filter_models_for_user`.

    The private models are only included if the *current* organisation
    still allows BYOK (``organizations.allow_byok``) — an org turning the
    toggle off hides this identity's private models while they're acting
    in that org, even though the same bindings keep working in any other
    org (or personal account) that still allows them.
    """

    from quberi_api.services import model_access as model_access_service

    all_models = await list_usable_chat_models(
        session, organization_id=organization_id
    )
    org_visible = await model_access_service.filter_models_for_user(
        session,
        organization_id=organization_id,
        user_id=user_id,
        models=all_models,
    )

    private_models: list[UsableChatModel] = []
    org_allows_byok = await _org_allows_byok(session, organization_id=organization_id)
    if org_allows_byok:
        private_bindings = await list_private_bindings_for_identity(
            session, identity_id=identity_id
        )
        for v in private_bindings:
            if v.provider_kind != "openai_compat":
                continue
            for mid in v.models:
                spec = MODEL_REGISTRY.get(mid)
                private_models.append(
                    UsableChatModel(
                        model_id=mid,
                        display_name=spec.display_name if spec else mid,
                        binding_id=v.id,
                        binding_name=v.display_name,
                        is_platform_default=False,
                        vendor=spec.vendor if spec else v.vendor,
                        is_private=True,
                    )
                )

    return [*org_visible, *private_models]


async def _org_allows_byok(session: AsyncSession, *, organization_id: uuid.UUID) -> bool:
    from quberi_api.domain.identity.models import Organization

    row = (
        await session.execute(
            select(Organization.allow_byok).where(Organization.id == organization_id)
        )
    ).scalar_one_or_none()
    return bool(row)


async def list_bindings_for_org(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> list[ProviderBindingView]:
    """Everything an org admin can see: its own shared bindings + any
    platform defaults. **Never** includes identity-private (BYOK)
    bindings — those carry ``organization_id = NULL`` and ``identity_id``
    set, so they simply never match this org-scoped query, let alone
    leak to an org admin. See :func:`list_private_bindings_for_identity`.

    Platform defaults are visible read-only so the settings UI can
    render "using Quberi-provided DeepSeek" instead of "no binding".
    """

    rows = (
        await session.execute(
            select(AiProviderBinding).where(
                (AiProviderBinding.organization_id == organization_id)
                | AiProviderBinding.is_platform_default.is_(True)
            )
        )
    ).scalars().all()
    return [_to_view(r) for r in rows]


def _clean_models(models: Optional[list]) -> list:
    if not models:
        return []
    seen: list = []
    for m in models:
        s = str(m).strip()
        if s and s not in seen:
            seen.append(s)
    return seen


async def register_binding(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    provider_kind: str,
    display_name: str,
    base_url: Optional[str],
    api_key: Optional[str],
    models: Optional[list] = None,
    extra_config: Optional[dict] = None,
    created_by_user_id: Optional[uuid.UUID] = None,
) -> ProviderBindingView:
    cfg = dict(extra_config or {})
    cfg["models"] = _clean_models(models)
    async with session.begin():
        row = AiProviderBinding(
            id=uuid.uuid4(),
            organization_id=organization_id,
            identity_id=None,
            provider_kind=provider_kind,
            display_name=display_name,
            base_url=base_url,
            api_key_encrypted=seal(api_key) if api_key else None,
            status="active",
            is_platform_default=False,
            extra_config=cfg,
            created_by_user_id=created_by_user_id,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row, attribute_names=["created_at", "updated_at"])
    return _to_view(row)


# ---------------------------------------------------------------------------
# CRUD — identity-private (BYOK) bindings
#
# A private binding carries ``organization_id = NULL`` and ``identity_id``
# set — it belongs to one global ``Identity``, not any particular
# organisation, so it is visible and usable from every org (and the
# personal account) that same person signs into. It is *never* returned
# by ``list_bindings_for_org`` and never reachable through any org-admin
# endpoint. Only the owning identity's own calls, scoped by
# ``identity_id`` in every WHERE clause below, can see or touch it.
#
# Whether a given org currently *allows* BYOK at all is a separate,
# per-org check (``organizations.allow_byok``) applied by the caller at
# read/use time — see ``list_usable_chat_models_for_user`` — not baked
# into these rows.
# ---------------------------------------------------------------------------


async def list_private_bindings_for_identity(
    session: AsyncSession, *, identity_id: uuid.UUID
) -> list[ProviderBindingView]:
    rows = (
        await session.execute(
            select(AiProviderBinding).where(
                AiProviderBinding.identity_id == identity_id,
            )
        )
    ).scalars().all()
    return [_to_view(r) for r in rows]


async def register_private_binding(
    session: AsyncSession,
    *,
    identity_id: uuid.UUID,
    provider_kind: str,
    display_name: str,
    base_url: Optional[str],
    api_key: Optional[str],
    models: Optional[list] = None,
    extra_config: Optional[dict] = None,
    created_by_user_id: Optional[uuid.UUID] = None,
) -> ProviderBindingView:
    cfg = dict(extra_config or {})
    cfg["models"] = _clean_models(models)
    async with session.begin():
        row = AiProviderBinding(
            id=uuid.uuid4(),
            organization_id=None,
            identity_id=identity_id,
            provider_kind=provider_kind,
            display_name=display_name,
            base_url=base_url,
            api_key_encrypted=seal(api_key) if api_key else None,
            status="active",
            is_platform_default=False,
            extra_config=cfg,
            created_by_user_id=created_by_user_id,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row, attribute_names=["created_at", "updated_at"])
    return _to_view(row)


async def update_private_binding(
    session: AsyncSession,
    *,
    identity_id: uuid.UUID,
    binding_id: uuid.UUID,
    display_name=_KEEP,
    base_url=_KEEP,
    api_key=_KEEP,
    models=_KEEP,
) -> Optional[ProviderBindingView]:
    """Edit one of *this identity's own* private bindings. ``None`` if not
    found or not owned by ``identity_id`` — same shape as :func:`update_binding`."""

    row = (
        await session.execute(
            select(AiProviderBinding).where(
                AiProviderBinding.id == binding_id,
                AiProviderBinding.identity_id == identity_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    await session.commit()
    async with session.begin():
        row = await session.get(AiProviderBinding, binding_id)
        if display_name is not _KEEP and str(display_name).strip():
            row.display_name = str(display_name).strip()
        if base_url is not _KEEP:
            row.base_url = (base_url or None)
        if api_key is not _KEEP and api_key:
            row.api_key_encrypted = seal(api_key)
        if models is not _KEEP:
            cfg = dict(row.extra_config or {})
            cfg["models"] = _clean_models(models)
            row.extra_config = cfg
        await session.flush()
        await session.refresh(row, attribute_names=["created_at", "updated_at"])
        view = _to_view(row)
    return view


async def delete_private_binding(
    session: AsyncSession,
    *,
    identity_id: uuid.UUID,
    binding_id: uuid.UUID,
) -> None:
    row = (
        await session.execute(
            select(AiProviderBinding).where(
                AiProviderBinding.id == binding_id,
                AiProviderBinding.identity_id == identity_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return
    await session.commit()
    async with session.begin():
        await session.delete(row)


async def probe_models_for_private_binding(
    session: AsyncSession,
    *,
    identity_id: uuid.UUID,
    binding_id: uuid.UUID,
) -> list:
    """Like :func:`probe_models_for_binding` but scoped to the identity's
    own private binding — never reaches into org-shared or other people's
    keys."""

    from quberi_api.services.security.crypto import unseal

    row = (
        await session.execute(
            select(AiProviderBinding).where(
                AiProviderBinding.id == binding_id,
                AiProviderBinding.identity_id == identity_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValueError("binding not found")
    if not row.base_url:
        raise ValueError("binding has no base_url to probe")
    key = unseal(row.api_key_encrypted) if row.api_key_encrypted else None
    return await probe_models(base_url=row.base_url, api_key=key)


async def probe_models(*, base_url: str, api_key: Optional[str]) -> list:
    """Fetch the OpenAI-compatible model list from ``base_url``.

    SECURITY: this issues an outbound HTTP GET to an admin-supplied URL
    with an admin-supplied key. It is a deliberate BYOK action (the admin
    is configuring their own endpoint), but it is an SSRF surface — keep
    the timeout short and never follow it with anything that trusts the
    response beyond a list of model-id strings.
    """

    from quberi_api.services.ai.base import ProviderConfig
    from quberi_api.services.ai.openai_compat import OpenAICompatProvider

    if not base_url:
        raise ValueError("base_url is required to fetch models")
    provider = OpenAICompatProvider(
        ProviderConfig(
            kind="openai_compat",
            display_name="probe",
            base_url=base_url,
            api_key=api_key,
            extra={},
        )
    )
    return await provider.list_models()


async def probe_models_for_binding(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    binding_id: uuid.UUID,
) -> list:
    """Fetch models for an existing binding, reusing its stored key.

    Only org-owned bindings or platform defaults (visible to the org) are
    probeable — mirrors the visibility of :func:`list_bindings_for_org`.
    """

    from quberi_api.services.security.crypto import unseal

    row = (
        await session.execute(
            select(AiProviderBinding).where(
                AiProviderBinding.id == binding_id,
                (AiProviderBinding.organization_id == organization_id)
                | AiProviderBinding.is_platform_default.is_(True),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise ValueError("binding not found")
    if not row.base_url:
        raise ValueError("binding has no base_url to probe")
    key = unseal(row.api_key_encrypted) if row.api_key_encrypted else None
    return await probe_models(base_url=row.base_url, api_key=key)


async def update_binding(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    binding_id: uuid.UUID,
    display_name=_KEEP,
    base_url=_KEEP,
    api_key=_KEEP,
    models=_KEEP,
) -> Optional[ProviderBindingView]:
    """Edit an *org-owned* binding. Returns None if not found / not editable.

    Only fields explicitly provided are touched. ``api_key`` is re-sealed
    only when a non-empty value is given (blank = keep the existing key,
    so the UI never has to re-enter it). Platform-default bindings have a
    NULL ``organization_id`` and are therefore never matched here.
    """

    row = (
        await session.execute(
            select(AiProviderBinding).where(
                AiProviderBinding.id == binding_id,
                AiProviderBinding.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    await session.commit()  # release read txn before the write txn
    async with session.begin():
        row = await session.get(AiProviderBinding, binding_id)
        if display_name is not _KEEP and str(display_name).strip():
            row.display_name = str(display_name).strip()
        if base_url is not _KEEP:
            row.base_url = (base_url or None)
        if api_key is not _KEEP and api_key:
            row.api_key_encrypted = seal(api_key)
        if models is not _KEEP:
            cfg = dict(row.extra_config or {})
            cfg["models"] = _clean_models(models)
            row.extra_config = cfg
        await session.flush()
        await session.refresh(row, attribute_names=["created_at", "updated_at"])
        view = _to_view(row)
    return view


async def delete_binding(
    session: AsyncSession, *, organization_id: uuid.UUID, binding_id: uuid.UUID
) -> None:
    row = (
        await session.execute(
            select(AiProviderBinding).where(
                AiProviderBinding.id == binding_id,
                AiProviderBinding.organization_id == organization_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return
    await session.commit()
    async with session.begin():
        await session.delete(row)


# ---------------------------------------------------------------------------
# CRUD — platform-default bindings (platform-admin only)
#
# Separate from the org-scoped CRUD above: a platform binding has
# ``organization_id = NULL`` and ``is_platform_default = True``, so it is
# never reachable through ``register_binding``/``update_binding``/
# ``delete_binding`` (all of which filter by ``organization_id ==
# <caller's org>``). These functions are only ever called from the
# platform-admin API surface (bearer-token gated, not an org session).
# ---------------------------------------------------------------------------


async def list_platform_bindings_full(
    session: AsyncSession,
) -> list[ProviderBindingView]:
    """Every platform-default binding, across every vendor — for the
    platform admin console's model-management page. Unlike
    :func:`list_bindings_for_org`, this is not scoped to any one org."""

    rows = (
        await session.execute(
            select(AiProviderBinding)
            .where(AiProviderBinding.is_platform_default.is_(True))
            .order_by(
                AiProviderBinding.provider_kind.asc(),
                AiProviderBinding.vendor.asc(),
                AiProviderBinding.priority.asc(),
            )
        )
    ).scalars().all()
    return [_to_view(r) for r in rows]


async def register_platform_binding(
    session: AsyncSession,
    *,
    provider_kind: str,
    vendor: Optional[str],
    display_name: str,
    base_url: Optional[str],
    api_key: Optional[str],
    priority: int = 100,
    models: Optional[list] = None,
    extra_config: Optional[dict] = None,
) -> ProviderBindingView:
    """Add a new platform-shared key. Several keys may share a
    ``(provider_kind, vendor)`` pair — see ``priority`` on the model."""

    cfg = dict(extra_config or {})
    cfg["models"] = _clean_models(models)
    async with session.begin():
        row = AiProviderBinding(
            id=uuid.uuid4(),
            organization_id=None,
            identity_id=None,
            provider_kind=provider_kind,
            vendor=vendor,
            display_name=display_name,
            base_url=base_url,
            api_key_encrypted=seal(api_key) if api_key else None,
            status="active",
            is_platform_default=True,
            priority=priority,
            extra_config=cfg,
        )
        session.add(row)
        await session.flush()
        await session.refresh(row, attribute_names=["created_at", "updated_at"])
    return _to_view(row)


async def update_platform_binding(
    session: AsyncSession,
    *,
    binding_id: uuid.UUID,
    display_name=_KEEP,
    base_url=_KEEP,
    api_key=_KEEP,
    status=_KEEP,
    priority=_KEEP,
    models=_KEEP,
) -> Optional[ProviderBindingView]:
    """Edit a platform-default binding (key rotation, pause/resume,
    re-priority). Returns ``None`` if ``binding_id`` isn't a platform
    binding."""

    row = (
        await session.execute(
            select(AiProviderBinding).where(
                AiProviderBinding.id == binding_id,
                AiProviderBinding.is_platform_default.is_(True),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    await session.commit()
    async with session.begin():
        row = await session.get(AiProviderBinding, binding_id)
        if display_name is not _KEEP and str(display_name).strip():
            row.display_name = str(display_name).strip()
        if base_url is not _KEEP:
            row.base_url = (base_url or None)
        if api_key is not _KEEP and api_key:
            row.api_key_encrypted = seal(api_key)
        if status is not _KEEP and status:
            row.status = str(status)
        if priority is not _KEEP and priority is not None:
            row.priority = int(priority)
        if models is not _KEEP:
            cfg = dict(row.extra_config or {})
            cfg["models"] = _clean_models(models)
            row.extra_config = cfg
        await session.flush()
        await session.refresh(row, attribute_names=["created_at", "updated_at"])
        view = _to_view(row)
    return view


async def delete_platform_binding(
    session: AsyncSession, *, binding_id: uuid.UUID
) -> None:
    row = (
        await session.execute(
            select(AiProviderBinding).where(
                AiProviderBinding.id == binding_id,
                AiProviderBinding.is_platform_default.is_(True),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return
    await session.commit()
    async with session.begin():
        await session.delete(row)


# ---------------------------------------------------------------------------
# Platform-admin: per-org platform model visibility
# ---------------------------------------------------------------------------


async def get_org_platform_model_visibility(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> tuple[str, list[str]]:
    """Return ``(mode, allowlisted_model_ids)`` for one org."""

    mode, allowed = await _platform_model_visibility_for_org(
        session, organization_id=organization_id
    )
    return mode, sorted(allowed) if allowed else []


async def set_org_platform_model_visibility(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    mode: str,
    model_ids: Optional[list[str]] = None,
) -> None:
    """Platform-admin action: set an org's platform-model visibility mode,
    and (for ``allowlist``) which model ids it may see.

    ``model_ids`` is ignored (and the allowlist table left untouched)
    for ``all``/``none`` — only ``allowlist`` reads it, and switching
    back to ``allowlist`` later re-applies whatever is stored, so a
    temporary switch to ``all`` doesn't lose the curated list.
    """

    if mode not in ("all", "none", "allowlist"):
        raise CodedValueError("admin.models.visibility_mode_invalid", mode=repr(mode))

    from quberi_api.domain.ai.models import OrganizationPlatformModelAllowlist
    from quberi_api.domain.identity.models import Organization

    org = await session.get(Organization, organization_id)
    if org is None:
        raise CodedValueError("admin.models.org_not_found", id=str(organization_id))

    await session.commit()
    async with session.begin():
        org = await session.get(Organization, organization_id)
        org.platform_model_visibility = mode
        if mode == "allowlist" and model_ids is not None:
            await session.execute(
                OrganizationPlatformModelAllowlist.__table__.delete().where(
                    OrganizationPlatformModelAllowlist.organization_id
                    == organization_id
                )
            )
            cleaned = _clean_models(model_ids)
            for mid in cleaned:
                session.add(
                    OrganizationPlatformModelAllowlist(
                        id=uuid.uuid4(),
                        organization_id=organization_id,
                        model_id=mid,
                    )
                )


# ---------------------------------------------------------------------------
# Model selections
# ---------------------------------------------------------------------------


async def get_selections_for_org(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> dict[str, dict]:
    """Return ``{purpose: {model_id, binding_id, ...}}`` for the UI.

    Purposes without an explicit row are reported as
    ``{"model_id": None, "binding_id": None}`` so the client can show
    "using platform default" for those slots.
    """

    rows = (
        await session.execute(
            select(AiModelSelection).where(
                AiModelSelection.organization_id == organization_id
            )
        )
    ).scalars().all()
    by_purpose = {r.purpose: r for r in rows}
    result: dict[str, dict] = {}
    for purpose in ("chat", "embedding", "reranker", "vision"):
        r = by_purpose.get(purpose)
        result[purpose] = {
            "model_id": r.model_id if r else None,
            "binding_id": str(r.provider_binding_id) if r else None,
        }
    return result


async def set_selection(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    purpose: str,
    model_id: str,
    binding_id: uuid.UUID,
) -> None:
    """Upsert one (org, purpose) selection.

    Validates ``model_id`` against the registry and checks that the
    binding's ``provider_kind`` matches the model's declared kind.
    """

    model = MODEL_REGISTRY.get(model_id)
    binding = (
        await session.execute(
            select(AiProviderBinding).where(AiProviderBinding.id == binding_id)
        )
    ).scalar_one_or_none()
    if binding is None:
        raise ValueError(f"binding {binding_id} not found")

    if purpose == "vision":
        # The vision model is a chat model that reads pictures. A listed one must say it
        # does; a name the administrator types for their own endpoint is taken on their word.
        if model is not None and not model.supports_vision:
            raise ValueError(f"model {model_id!r} does not read pictures")
        if binding.provider_kind != "openai_compat":
            raise ValueError(
                f"a vision model needs an openai_compat binding, not {binding.provider_kind}"
            )
        if binding.identity_id is not None:
            raise ValueError("a member's private key cannot be the organisation's vision model")
    elif model is not None:
        # Known model → enforce purpose + provider_kind match.
        if model.purpose != purpose:
            raise ValueError(
                f"model {model_id!r} is for purpose {model.purpose!r}, not {purpose!r}"
            )
        if binding.provider_kind != model.provider_kind:
            raise ValueError(
                f"binding {binding.display_name!r} is {binding.provider_kind}, "
                f"but model {model_id} requires {model.provider_kind}"
            )
    else:
        # Unknown model id: only chat may be arbitrary (BYOK endpoints
        # expose their own names); embedding/reranker must be registered.
        if purpose != "chat":
            raise ValueError(f"unknown {purpose} model {model_id!r}")
        if binding.provider_kind != "openai_compat":
            raise ValueError(
                f"custom chat models require an openai_compat binding, "
                f"not {binding.provider_kind}"
            )
    # Bindings owned by another org cannot be selected.
    if (
        binding.organization_id is not None
        and binding.organization_id != organization_id
    ):
        raise ValueError("cannot select a binding owned by another organization")

    existing = (
        await session.execute(
            select(AiModelSelection).where(
                AiModelSelection.organization_id == organization_id,
                AiModelSelection.purpose == purpose,
            )
        )
    ).scalar_one_or_none()

    changed = existing is None or (existing.model_id, existing.provider_binding_id) != (
        model_id,
        binding_id,
    )
    await session.commit()
    async with session.begin():
        if purpose == "vision" and changed:
            # Pictures go to the model's company, which the owner agreed to for the model that
            # was chosen. A different model is a different company: ask again.
            await _switch_off_picture_descriptions(session, organization_id)
        if existing is not None:
            existing.provider_binding_id = binding_id
            existing.model_id = model_id
        else:
            session.add(
                AiModelSelection(
                    id=uuid.uuid4(),
                    organization_id=organization_id,
                    purpose=purpose,
                    provider_binding_id=binding_id,
                    model_id=model_id,
                )
            )


async def _switch_off_picture_descriptions(
    session: AsyncSession, organization_id: uuid.UUID
) -> None:
    """Turn the picture-description switch off (and say so in the audit log), if it is on.

    Runs inside the caller's write transaction.
    """

    from quberi_api.domain.audit.models import AuditEvent
    from quberi_api.domain.identity.models import Organization

    org = await session.get(Organization, organization_id)
    if org is None or not org.image_vision_enabled:
        return
    org.image_vision_enabled = False
    session.add(
        AuditEvent(
            organization_id=organization_id,
            actor_type="system",
            actor_id=None,
            action="organization.image_vision_changed",
            resource_type="organization",
            resource_id=organization_id,
            payload={"enabled": False, "reason": "vision model changed"},
        )
    )


async def clear_selection(
    session: AsyncSession, *, organization_id: uuid.UUID, purpose: str
) -> None:
    """Remove the explicit selection so the org falls back to platform default."""

    existing = (
        await session.execute(
            select(AiModelSelection).where(
                AiModelSelection.organization_id == organization_id,
                AiModelSelection.purpose == purpose,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        return
    await session.commit()
    async with session.begin():
        if purpose == "vision":
            await _switch_off_picture_descriptions(session, organization_id)
        await session.delete(existing)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


async def bootstrap_platform_defaults(
    session: AsyncSession, *, settings: Settings
) -> None:
    """Ensure the platform-default bindings match the current env vars.

    Runs once per boot in the lifespan hook. Idempotent — if the desired
    binding already exists and matches, nothing changes; if the key was
    rotated in ``.env``, the ciphertext is updated in place.

    Several *vendors* can each have their own platform-default binding
    (e.g. DeepSeek and OpenAI side by side) — they share
    ``provider_kind='openai_compat'`` but are told apart by ``vendor``,
    so bootstrapping one never collides with or overwrites the other.
    """

    # ---- Local embedding (BGE-M3) — always available, no credential ----
    await _ensure_platform_binding(
        session,
        provider_kind="local_embedding",
        vendor=None,
        display_name="Quberi local embeddings (BGE-M3)",
        base_url=None,
        api_key=None,
        extra_config={
            "description": "Bundled sentence-transformers loader.",
            "models": [
                m.model_id
                for m in MODEL_REGISTRY.values()
                if m.provider_kind == "local_embedding"
            ],
        },
    )

    # ---- DeepSeek chat — only if a key is configured ----
    await _bootstrap_vendor_chat_binding(
        session,
        vendor="deepseek",
        display_name="Quberi shared DeepSeek",
        base_url=DEFAULT_DEEPSEEK_BASE_URL,
        key_setting=settings.default_deepseek_key,
        env_var_name="QUBERI_DEFAULT_DEEPSEEK_KEY",
        models=[m.model_id for m in models_for_vendor("deepseek") if m.purpose == "chat"],
    )

    # ---- OpenAI chat — only if a key is configured ----
    await _bootstrap_vendor_chat_binding(
        session,
        vendor="openai",
        display_name="Quberi shared OpenAI",
        base_url=DEFAULT_OPENAI_BASE_URL,
        key_setting=settings.default_openai_key,
        env_var_name="QUBERI_DEFAULT_OPENAI_KEY",
        models=[m.model_id for m in models_for_vendor("openai") if m.purpose == "chat"],
    )


async def _bootstrap_vendor_chat_binding(
    session: AsyncSession,
    *,
    vendor: str,
    display_name: str,
    base_url: str,
    key_setting,
    env_var_name: str,
    models: list[str],
) -> None:
    if not key_setting:
        _log.info(
            "%s unset; no platform %s chat binding will be provisioned",
            env_var_name, vendor,
        )
        return
    key = key_setting.get_secret_value().strip()
    if not key:
        _log.info(
            "%s is empty; skipping platform %s binding", env_var_name, vendor
        )
        return
    await _ensure_platform_binding(
        session,
        provider_kind="openai_compat",
        vendor=vendor,
        display_name=display_name,
        base_url=base_url,
        api_key=key,
        extra_config={
            "description": "Platform-provided fallback. Set your own "
            "OpenAI-compat key to opt out.",
            "models": models,
        },
    )


async def _ensure_platform_binding(
    session: AsyncSession,
    *,
    provider_kind: str,
    vendor: Optional[str],
    display_name: str,
    base_url: Optional[str],
    api_key: Optional[str],
    extra_config: dict,
) -> None:
    """Create or update the one platform-default binding for
    ``(provider_kind, vendor)``.

    Bootstrap only ever manages *one* key per vendor (the one named by
    its ``QUBERI_DEFAULT_*_KEY`` env var); additional keys for the same
    vendor (fallback/overflow, different billing accounts) are added by
    hand through the platform admin console and are left untouched here
    — this lookup is scoped to rows with no ``extra_config.managed_by``
    marker conflict by always targeting the single bootstrap-owned row
    (the first one found), identified by vendor + provider_kind.
    """

    existing = (
        await session.execute(
            select(AiProviderBinding).where(
                AiProviderBinding.is_platform_default.is_(True),
                AiProviderBinding.provider_kind == provider_kind,
                AiProviderBinding.vendor == vendor,
            )
        )
    ).scalar_one_or_none()

    sealed = seal(api_key) if api_key else None

    # The SELECT above implicitly began a read transaction; close it so
    # we can start a fresh write transaction below without SQLAlchemy
    # complaining about "already begun".
    await session.commit()

    if existing is None:
        _log.info("bootstrapping platform default binding: %s", display_name)
        async with session.begin():
            session.add(
                AiProviderBinding(
                    id=uuid.uuid4(),
                    organization_id=None,
                    provider_kind=provider_kind,
                    vendor=vendor,
                    display_name=display_name,
                    base_url=base_url,
                    api_key_encrypted=sealed,
                    status="active",
                    is_platform_default=True,
                    priority=100,
                    extra_config=extra_config,
                )
            )
        return

    # Update in place if any observable field drifted.
    changed = False
    if existing.display_name != display_name:
        existing.display_name = display_name
        changed = True
    if existing.base_url != base_url:
        existing.base_url = base_url
        changed = True
    if existing.api_key_encrypted != sealed:
        # We compare ciphertext, which changes on every seal() call
        # because Fernet includes an IV. Only update when the plaintext
        # has actually changed — detect this by comparing "is a key
        # present" for now, and re-seal only if the caller has provided
        # a new plaintext key.
        if api_key is not None:
            existing.api_key_encrypted = sealed
            changed = True
        elif api_key is None and existing.api_key_encrypted is not None:
            existing.api_key_encrypted = None
            changed = True
    # ``models`` is edited by admins in the console and is the routing
    # source of truth, so a restart must never reset it to the seed value.
    # The seed list is only used when the row has none yet.
    desired_config = dict(extra_config)
    if existing.extra_config and "models" in existing.extra_config:
        desired_config["models"] = existing.extra_config["models"]
    if existing.extra_config != desired_config:
        existing.extra_config = desired_config
        changed = True
    if existing.status != "active":
        existing.status = "active"
        changed = True
    if changed:
        _log.info("updated platform default binding: %s", display_name)
        await session.commit()
