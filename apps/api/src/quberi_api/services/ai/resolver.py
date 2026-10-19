"""Turn an org id + purpose into a usable provider instance.

Resolution order
----------------
1. Look up the org's :class:`AiModelSelection` for the requested
   ``purpose``. If found, follow its ``provider_binding_id`` and build a
   provider from that binding + the row's ``model_id``.
2. Fall back to a platform-default binding whose ``provider_kind`` can
   serve the requested purpose *and* whose linked model matches the
   platform-default env vars (``QUBERI_DEFAULT_CHAT_MODEL`` /
   ``QUBERI_DEFAULT_EMBEDDING_MODEL``).
3. If neither exists, raise :class:`ProviderResolutionError` — the
   caller (usually the RAG service) surfaces it as a 503 to signal
   "an admin needs to configure a provider".
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.ai.models import AiModelSelection, AiProviderBinding
from quberi_api.services.ai.base import (
    ChatProvider,
    EmbeddingProvider,
    ProviderConfig,
)
from quberi_api.services.ai.registry import MODEL_REGISTRY, ModelSpec
from quberi_api.services.security.crypto import unseal


class ProviderResolutionError(RuntimeError):
    """Raised when no binding can serve the requested (org, purpose)."""


class BindingUnavailableError(ProviderResolutionError):
    """A specifically chosen binding no longer exists, is hidden from the caller or is off.

    Kept apart from the parent so a caller can fall back to the default model for this
    case only. A binding that exists but cannot do chat is a setup mistake, and stays a
    plain :class:`ProviderResolutionError` so it is not hidden by a silent model switch.
    """


@dataclass(frozen=True)
class ResolvedProvider:
    """The result of :func:`resolve_chat_provider` / :func:`resolve_embedding_provider`.

    Carries the model spec alongside the ready-to-call provider so
    downstream code (e.g. the retriever) can size its Qdrant collection
    and prompt budgets without re-looking-up the registry.
    """

    provider: object  # ChatProvider | EmbeddingProvider
    model: ModelSpec
    binding_id: uuid.UUID
    is_platform_default: bool
    # True when the model the conversation asked for was gone and this is the default
    # instead (see ``rag.generate._resolve_chat``).
    fallback: bool = False


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


async def resolve_chat_provider(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> ResolvedProvider:
    resolved = await _resolve(session, organization_id=organization_id, purpose="chat")
    provider = _instantiate(resolved.binding, resolved.model)
    if not isinstance(provider, ChatProvider):
        raise ProviderResolutionError(
            f"provider {resolved.binding.display_name!r} can't do chat "
            f"(kind={resolved.binding.provider_kind})"
        )
    return ResolvedProvider(
        provider=provider,
        model=resolved.model,
        binding_id=resolved.binding.id,
        is_platform_default=resolved.binding.is_platform_default,
    )


async def resolve_embedding_provider(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> ResolvedProvider:
    resolved = await _resolve(
        session, organization_id=organization_id, purpose="embedding"
    )
    provider = _instantiate(resolved.binding, resolved.model)
    if not isinstance(provider, EmbeddingProvider):
        raise ProviderResolutionError(
            f"provider {resolved.binding.display_name!r} can't do embedding "
            f"(kind={resolved.binding.provider_kind})"
        )
    return ResolvedProvider(
        provider=provider,
        model=resolved.model,
        binding_id=resolved.binding.id,
        is_platform_default=resolved.binding.is_platform_default,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Resolved:
    binding: AiProviderBinding
    model: ModelSpec


async def _resolve(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    purpose: str,
) -> _Resolved:
    # 1. Explicit org-level selection wins.
    selection = (
        await session.execute(
            select(AiModelSelection).where(
                AiModelSelection.organization_id == organization_id,
                AiModelSelection.purpose == purpose,
            )
        )
    ).scalar_one_or_none()

    if selection is not None:
        binding = (
            await session.execute(
                select(AiProviderBinding).where(
                    AiProviderBinding.id == selection.provider_binding_id
                )
            )
        ).scalar_one_or_none()
        if binding is None or binding.status != "active":
            raise ProviderResolutionError(
                f"org {organization_id} selection references a "
                f"missing/inactive binding for {purpose}"
            )
        # Unknown chat model ids are allowed (BYOK endpoints expose arbitrary
        # names). Embedding/reranker must be registry-known for dim/collection.
        model = _spec_for(selection.model_id, binding.provider_kind, purpose)
        return _Resolved(binding=binding, model=model)

    # 2. Platform default. Pick the model the env vars name so ops has
    # explicit control over what the fallback is.
    default_model_id = _default_model_env(purpose)
    if default_model_id is None:
        raise ProviderResolutionError(
            f"no {purpose} model selected for org {organization_id} and no "
            f"platform default is configured (set "
            f"QUBERI_DEFAULT_{purpose.upper()}_MODEL)"
        )
    
    # Try to find a binding that serves this model
    binding = await _pick_platform_binding(session, model_id=default_model_id)
    if binding is None:
        raise ProviderResolutionError(
            f"no active platform-default binding serves {purpose} model "
            f"{default_model_id!r}"
        )
    
    # Get model spec from registry if available, else create synthetic
    model = _spec_for(default_model_id, binding.provider_kind, purpose)
    return _Resolved(binding=binding, model=model)


async def _pick_platform_binding(
    session: AsyncSession,
    *,
    model_id: str,
) -> Optional[AiProviderBinding]:
    """Pick the best active platform-default binding that serves this model.

    Bindings store their model list in extra_config.models. We pick the
    active binding with the lowest priority that contains this model_id.
    This eliminates the need for vendor matching — a binding's model list
    is the source of truth for what it can serve.
    """

    rows = (
        await session.execute(
            select(AiProviderBinding)
            .where(
                AiProviderBinding.is_platform_default.is_(True),
                AiProviderBinding.status == "active",
            )
            .order_by(AiProviderBinding.priority.asc(), AiProviderBinding.created_at.asc())
        )
    ).scalars().all()
    
    # Filter to bindings that contain this model_id in their models list
    for binding in rows:
        models = []
        if binding.extra_config and "models" in binding.extra_config:
            models = binding.extra_config["models"]
        if model_id in models:
            return binding

    # Locally-run models (bge-m3, rerankers) have no external vendor and no
    # credential, so their binding doesn't need an explicit models list:
    # any active vendor-less binding of the same provider_kind serves them.
    spec = MODEL_REGISTRY.get(model_id)
    if spec is not None and spec.vendor is None:
        for binding in rows:
            if binding.vendor is None and binding.provider_kind == spec.provider_kind:
                return binding

    return None


def _default_model_env(purpose: str) -> Optional[str]:
    return os.getenv(f"QUBERI_DEFAULT_{purpose.upper()}_MODEL") or None


def _spec_for(model_id: str, provider_kind: str, purpose: str) -> ModelSpec:
    """Registry spec if known, else a synthetic one (chat only).

    Chat models can be arbitrary strings a BYOK endpoint exposes, so we
    don't require them in the registry. Embedding/reranker still must be
    registry-known (we need vector_dim / the local loader), so an unknown
    one raises.
    """

    known = MODEL_REGISTRY.get(model_id)
    if known is not None:
        return known
    if purpose != "chat":
        raise ProviderResolutionError(
            f"unknown {purpose} model {model_id!r} (must be a registered model)"
        )
    return ModelSpec(
        model_id=model_id,
        provider_kind=provider_kind,
        purpose="chat",
        display_name=model_id,
        context_window=8192,
    )


async def resolve_chat_provider_explicit(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    identity_id: Optional[uuid.UUID],
    binding_id: uuid.UUID,
    model_id: str,
) -> ResolvedProvider:
    """Build a chat provider from a specific (binding, model) pair.

    Used by the per-conversation model override. The binding must be one
    of: this org's own binding, a platform default, or the caller's own
    identity-private BYOK binding (``identity_id`` matches) — the latter
    is why ``identity_id`` is required here even though most callers only
    ever hit the org/platform branches.
    """

    visibility = [
        AiProviderBinding.organization_id == organization_id,
        AiProviderBinding.is_platform_default.is_(True),
    ]
    if identity_id is not None:
        visibility.append(AiProviderBinding.identity_id == identity_id)

    binding = (
        await session.execute(
            select(AiProviderBinding).where(
                AiProviderBinding.id == binding_id,
                or_(*visibility),
            )
        )
    ).scalar_one_or_none()
    if binding is None or binding.status != "active":
        raise BindingUnavailableError("chosen model binding not found or inactive")
    model = _spec_for(model_id, binding.provider_kind, "chat")
    provider = _instantiate(binding, model)
    if not isinstance(provider, ChatProvider):
        raise ProviderResolutionError(
            f"binding {binding.display_name!r} can't do chat"
        )
    return ResolvedProvider(
        provider=provider,
        model=model,
        binding_id=binding.id,
        is_platform_default=binding.is_platform_default,
    )


def _instantiate(binding: AiProviderBinding, model: ModelSpec):
    """Build a fresh provider from a binding + model spec.

    We intentionally rebuild per call — providers are cheap value
    objects; heavy state (loaded ML models) lives in module-level caches
    inside each provider implementation.
    """

    config = ProviderConfig(
        kind=binding.provider_kind,
        display_name=binding.display_name,
        base_url=binding.base_url,
        api_key=unseal(binding.api_key_encrypted) if binding.api_key_encrypted else None,
        extra=binding.extra_config or {},
    )
    if binding.provider_kind == "openai_compat":
        # Deferred imports so heavy deps don't cost boot time on
        # embed-only workers etc.
        from quberi_api.services.ai.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(
            config, vector_dim=model.vector_dim or 1536
        )
    if binding.provider_kind == "local_embedding":
        from quberi_api.services.ai.local_embedding import LocalEmbeddingProvider

        return LocalEmbeddingProvider(
            config, vector_dim=model.vector_dim or 1024
        )
    raise ProviderResolutionError(
        f"unknown provider_kind {binding.provider_kind!r}"
    )
