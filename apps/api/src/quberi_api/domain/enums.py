"""String enumerations used in check constraints.

We deliberately store these as text columns with ``CheckConstraint`` rather than
Postgres native enum types because native enums are painful to evolve (adding
values requires ``ALTER TYPE`` outside a transaction; removing values requires
a full type rebuild). Text + check constraint gives us the same guarantees with
migrations that are boring to review.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Lifecycle statuses
# ---------------------------------------------------------------------------
ORGANIZATION_STATUS = ("active", "suspended", "archived")
# Product tier: ``personal`` = self-serve single-member org (C-end, org concepts
# hidden); ``team`` = multi-member enterprise org (default, backwards compatible).
ORGANIZATION_TIERS = ("personal", "team")
IDENTITY_STATUS = ("active", "disabled")
USER_STATUS = ("invited", "active", "suspended", "archived")
SERVICE_ACCOUNT_STATUS = ("active", "disabled")
GENERIC_STATUS = ("active", "archived")

# ---------------------------------------------------------------------------
# Identity providers
# ---------------------------------------------------------------------------
IDENTITY_PROVIDERS = (
    "password",
    "saml",
    "oidc",
    "lark",
    "wechat_work",
    "dingtalk",
    "google",
    "microsoft",
    "github",
)

# ---------------------------------------------------------------------------
# Organisation graph
# ---------------------------------------------------------------------------
ORG_UNIT_TYPES = (
    "department",
    "team",
    "business_unit",
    "legal_entity",
    "cost_center",
    "custom",
)

GROUP_TYPES = ("static", "dynamic", "security", "project")
GROUP_MEMBER_TYPES = ("user", "organization_unit", "position", "service_account")

# ---------------------------------------------------------------------------
# Spaces
# ---------------------------------------------------------------------------
SPACE_PRESETS = ("personal", "collaborative", "managed")
SPACE_OWNER_TYPES = ("user", "group", "organization_unit", "organization")
SPACE_VISIBILITY = ("private", "organization", "public_link")
SPACE_TRUST_LEVELS = ("trusted", "guided", "exploratory")

# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------
ROLE_SUBJECT_TYPES = (
    "user",
    "group",
    "organization_unit",
    "position",
    # "Everyone in the organisation": ``subject_id`` is the organisation's
    # own id. Lets an admin share a space with the whole enterprise without
    # maintaining an all-staff group.
    "organization",
    "service_account",
)
ROLE_RESOURCE_TYPES = ("organization", "space", "agent")

# ---------------------------------------------------------------------------
# Model access governance
# ---------------------------------------------------------------------------
# ``allow`` grants a principal the right to use the model; ``deny`` revokes
# it even if the same person is also covered by an ``allow`` grant through
# a different principal (e.g. their group). Deny always wins — see
# ``services.model_access`` for the two-pass evaluation order.
MODEL_ACCESS_POLICY_EFFECTS = ("allow", "deny")

# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------
# How an agent resolves which knowledge spaces its ``search_knowledge`` tool
# may read:
#   ``user``      — every space the *calling user* can access (default;
#                   permission-faithful, the agent never out-reaches them).
#   ``intersect`` — the agent's configured ``scope_space_ids`` intersected
#                   with the caller's accessible spaces (a narrowed, still
#                   permission-faithful subset).
#   ``fixed``     — exactly the agent's configured ``scope_space_ids``,
#                   independent of the caller's own access. This lets an
#                   agent mediate knowledge a user could not otherwise read,
#                   so it is only safe when paired with a restricted
#                   ``visibility``; the config layer flags the combination.
AGENT_KNOWLEDGE_SCOPE_MODES = ("user", "intersect", "fixed")

# Who may see + run an agent:
#   ``organization`` — every member of the org (default).
#   ``restricted``   — only subjects granted access via a RoleBinding on the
#                      agent (plus org owners/admins, who manage all agents).
AGENT_VISIBILITY = ("organization", "restricted")

# ---------------------------------------------------------------------------
# Positions (job roles / establishment slots)
# ---------------------------------------------------------------------------
# ``active``   — normal, can accept new occupants
# ``frozen``   — 冻编: no new hires, existing tenures survive
# ``archived`` — 废除: no occupants, hidden from default listings
POSITION_STATUS = ("active", "frozen", "archived")

# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
AUDIT_ACTOR_TYPES = ("user", "service_account", "system")

# ---------------------------------------------------------------------------
# Directory sync state (per-entity)
# ---------------------------------------------------------------------------
# ``local``            — created in Quberi, no external counterpart
# ``synced``           — mirrored from an external directory (read-only in UI)
# ``synced_augmented`` — mirrored, but Quberi has added attributes the source
#                        does not know about (source-owned fields stay read-only)
SYNC_STATES = ("local", "synced", "synced_augmented")

# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------
CONVERSATION_STATUS = ("active", "archived")

# Knowledge scope for a conversation:
#   ``auto``   — RAG searches every space the caller has viewer+ access to,
#                re-evaluated at query time (default).
#   ``custom`` — RAG searches only the spaces explicitly attached via the
#                ``conversation_knowledge_sources`` link table (still
#                filtered against the caller's live access).
CONVERSATION_SCOPE_MODES = ("auto", "custom")

# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------
# ``uploaded``   — file safely landed in object storage, DB row committed
# ``processing`` — reserved: text extraction / embedding in flight (RAG slice)
# ``failed``     — upload aborted mid-flight, DB row exists for auditability
# ``archived``   — soft-deleted; row remains for audit trail, file removed
DOCUMENT_STATUS = ("uploaded", "processing", "failed", "archived")

# Source-of-truth for a document. Anything but ``local`` is expected to be
# fetched from an external connector.
DOCUMENT_SOURCE_KINDS = ("local", "feishu", "oss_aliyun", "s3_aws")

# Life-cycle of a source-registered document *relative to its source*.
#   ``local_only``     — this document has no external source (uploaded here)
#   ``synced_fresh``   — mirrored copy is up to date w.r.t. source at last check
#   ``synced_stale``   — source was modified after our last sync; needs refetch
#   ``synced_broken``  — source no longer reachable / permission gone
DOCUMENT_SYNC_STATES = (
    "local_only",
    "synced_fresh",
    "synced_stale",
    "synced_broken",
)

# Health of a registered ``document_source`` connection.
DOCUMENT_SOURCE_STATUS = ("active", "paused", "error", "archived")

# Whether the connector mirrors bytes into MinIO on sync, or keeps only a
# reference back to the origin.
#   ``mirror``          — default; download bytes into MinIO for durability +
#                         deterministic re-embedding
#   ``reference_only``  — compliance / very-large-blob mode; only metadata
#                         and external_ref stored, RAG streams through at
#                         ingest time
DOCUMENT_CACHING_POLICY = ("mirror", "reference_only")

# RAG indexing pipeline state for a single document (Slice G groundwork —
# the actual indexer lands in a later slice; today every row is
# ``not_indexed`` and the UI reflects it).
#   ``not_indexed`` — never processed
#   ``indexing``    — worker holds a lease and is embedding chunks
#   ``indexed``     — successfully embedded, retrievable
#   ``failed``      — last attempt raised; see ``indexing_error``
DOCUMENT_INDEXING_STATES = ("not_indexed", "indexing", "indexed", "failed")

# ``system`` and ``tool`` are reserved for later slices (RAG / agents) but
# ship in the check constraint from day 1 so we do not need a migration when
# they start being used.
MESSAGE_ROLES = ("user", "assistant", "system", "tool")
MESSAGE_STATUS = ("pending", "completed", "failed")
