"""AI provider bindings + per-org model selections.

Design note — ``provider_kind`` is *deliberately* an open string. The
authoritative catalog lives in :mod:`quberi_api.services.ai.registry`
(application code, not the DB) so adding an XAI / Cohere / whatever
provider takes a Python file, not a migration. The check constraints
on this table are only on lifecycle enums (``status``, ``purpose``)
where the set of legal values is genuinely small and stable.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from quberi_api.db.base import Base
from quberi_api.db.types import TimestampMixin, UUIDPk


class AiProviderBinding(TimestampMixin, Base):
    """A credentialled connection to some LLM / embedding provider.

    Rows are one of three shapes:

    * **Platform-scoped** (``organization_id`` NULL, ``identity_id`` NULL,
      ``is_platform_default = True``) — Quberi-provided fallback,
      auto-created at API boot from ``QUBERI_DEFAULT_*`` env vars.
    * **Organisation-scoped** (``organization_id`` set, ``identity_id``
      NULL) — the org brought its own key and controls its own quota;
      shared by every member of that org.
    * **Identity-scoped** (``organization_id`` NULL, ``identity_id`` set)
      — one person's private BYOK key. Deliberately keyed off the global
      ``Identity``, not a per-org ``User`` row: the same human may act
      inside several organisations (plus their personal account) as
      distinct ``User`` rows, but it's still *their* key, so it must be
      visible and usable from every one of those sessions. Whether it can
      actually be *used* in a given org is re-checked at read time against
      that org's ``allow_byok`` toggle — the row itself carries no org tie
      to enforce.

    Credentials are stored via
    :func:`quberi_api.services.security.crypto.seal` and never leak to
    the API surface (the read Pydantic schema returns a boolean
    ``credentials_present`` instead).
    """

    __tablename__ = "ai_provider_bindings"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'paused', 'error', 'archived')",
            name="ck_ai_provider_bindings_status",
        ),
        # Three legal shapes — see the class docstring.
        CheckConstraint(
            "(is_platform_default = TRUE AND organization_id IS NULL AND identity_id IS NULL) OR "
            "(is_platform_default = FALSE AND organization_id IS NOT NULL AND identity_id IS NULL) OR "
            "(is_platform_default = FALSE AND organization_id IS NULL AND identity_id IS NOT NULL)",
            name="ck_ai_provider_bindings_scope",
        ),
        # NOT unique — a vendor may have several platform-default keys
        # (e.g. two DeepSeek keys on different billing accounts for
        # headroom/failover). ``vendor`` distinguishes DeepSeek from
        # OpenAI even though both share ``provider_kind=openai_compat``;
        # ``priority`` (lower = tried first) breaks ties within one
        # vendor. See :func:`services.ai.resolver._pick_platform_binding`.
        Index(
            "ix_ai_provider_bindings_platform_default_kind_vendor",
            "provider_kind",
            "vendor",
            postgresql_where="is_platform_default = TRUE",
        ),
        Index(
            "ix_ai_provider_bindings_organization_id",
            "organization_id",
        ),
        Index(
            "ix_ai_provider_bindings_identity_id",
            "identity_id",
        ),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Set only for an identity-private (BYOK) binding. NULL for both
    # platform-default and org-shared bindings. Points at the global
    # ``Identity`` (not a per-org ``User``) so the binding follows the
    # person across every organisation and their personal account.
    identity_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("identities.id", ondelete="CASCADE"),
        nullable=True,
    )
    provider_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Which company this binding talks to — e.g. ``deepseek``, ``openai``,
    # ``dashscope``. Several ``provider_kind`` values (today: just
    # ``openai_compat``) are shared by multiple companies that happen to
    # speak the same wire protocol, so ``provider_kind`` alone can't tell
    # DeepSeek apart from OpenAI. NULL for ``local_embedding`` /
    # ``local_reranker`` bindings, which have no external vendor.
    # Org-owned and identity-private bindings may also set this (purely
    # informational there); only platform-default rows rely on it to stay
    # distinct per vendor.
    vendor: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    api_key_encrypted: Mapped[Optional[str]] = mapped_column(
        String(4096), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active"
    )
    is_platform_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    # Lower tries first among multiple active platform-default bindings
    # for the same (provider_kind, vendor) — e.g. two DeepSeek keys on
    # separate billing accounts. Ties broken by ``created_at``. Irrelevant
    # for org-owned/private bindings (an org/person has at most one active
    # binding per provider in practice; nothing currently reads this
    # outside the platform-default resolution path).
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100, server_default="100"
    )
    extra_config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_by_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )


class AiModelSelection(TimestampMixin, Base):
    """The org's choice of *which* binding + model to use for a given purpose.

    One row per (organization, purpose). ``resolve_provider(org, purpose)``
    reads this table first; if no row exists, the resolver falls back to
    the platform default binding for the purpose (looking up
    ``is_platform_default=True`` bindings and picking one whose ``kind``
    can serve that purpose).
    """

    __tablename__ = "ai_model_selections"
    __table_args__ = (
        CheckConstraint(
            "purpose IN ('chat', 'embedding', 'reranker', 'vision')",
            name="ck_ai_model_selections_purpose",
        ),
        UniqueConstraint(
            "organization_id",
            "purpose",
            name="uq_ai_model_selections_organization_purpose",
        ),
        Index(
            "ix_ai_model_selections_provider_binding_id",
            "provider_binding_id",
        ),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_binding_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai_provider_bindings.id", ondelete="RESTRICT"),
        nullable=False,
    )
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)


class ModelAccessPolicy(Base):
    """Which principals may (or may not) use a specific (binding, model)
    chat model.

    Enterprise governance. Semantics: a model with **no** policy rows is
    open to the whole org. Any ``allow`` row restricts it to the listed
    principals (plus org owners/admins, who bypass). An ``effect='deny'``
    row *excludes* that principal even if they are also covered by an
    ``allow`` grant through a different principal (e.g. their group) —
    deny always wins; see ``services.model_access`` for the evaluation
    order. Principals reuse the platform's subject types so we can grant
    or deny a single user, a group, an organization unit, or a position.
    """

    __tablename__ = "model_access_policies"
    __table_args__ = (
        CheckConstraint(
            "principal_type IN ('user', 'group', 'organization_unit', 'position')",
            name="ck_model_access_policies_principal_type",
        ),
        CheckConstraint(
            "effect IN ('allow', 'deny')",
            name="ck_model_access_policies_effect",
        ),
        UniqueConstraint(
            "organization_id",
            "provider_binding_id",
            "model_id",
            "principal_type",
            "principal_id",
            name="uq_model_access_policies_grant",
        ),
        Index(
            "ix_model_access_policies_model",
            "organization_id",
            "provider_binding_id",
            "model_id",
        ),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider_binding_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai_provider_bindings.id", ondelete="CASCADE"),
        nullable=False,
    )
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    principal_type: Mapped[str] = mapped_column(String(32), nullable=False)
    principal_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    # ``allow`` (default, matches every pre-existing row) or ``deny``.
    effect: Mapped[str] = mapped_column(
        String(8), nullable=False, default="allow", server_default="allow"
    )


class OrganizationPlatformModelAllowlist(TimestampMixin, Base):
    """Which platform-shared chat models a specific org may see at all.

    Only consulted when ``organizations.platform_model_visibility ==
    'allowlist'`` — a row here for ``model_id`` means "this org's model
    picker may show this platform model"; the absence of a row means
    hidden. Irrelevant (and never read) under the ``all``/``none`` modes.
    This is a platform-admin-only governance layer that sits *above*
    ``ModelAccessPolicy`` — a model must clear this allowlist (or the org
    must be in ``all`` mode) before per-member policy is even evaluated.

    ``model_id`` is a free string (not FK'd to anything) matching the
    registry's ``model_id``, same convention as ``AiModelSelection`` and
    ``ModelAccessPolicy``.
    """

    __tablename__ = "organization_platform_model_allowlist"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "model_id",
            name="uq_organization_platform_model_allowlist_org_model",
        ),
        Index(
            "ix_organization_platform_model_allowlist_organization_id",
            "organization_id",
        ),
    )

    id: Mapped[UUIDPk]
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
    )
    model_id: Mapped[str] = mapped_column(String(200), nullable=False)
