"""Built-in permission catalogue and system roles.

Two contracts live here:

* ``PERMISSIONS`` — the *language* the platform speaks about access. Codes are
  namespaced ``domain:action`` and never change once shipped (renaming a code
  would silently break existing ``RoleBinding`` rows in customer databases).
* ``SYSTEM_ROLES`` — the default bundles every organisation gets. Marked
  ``is_system = True`` and reserved via ``organization_id IS NULL`` so
  customers cannot delete or rename them; they can however create custom roles
  that combine the same permissions.

Both structures are consumed by:

* The 0002 seed migration (writes rows into ``permissions`` / ``roles`` /
  ``role_permissions``).
* The onboarding service (looks up the ``owner`` role by ``code`` to grant it
  to the first user of a new organisation).

Add-only rules
--------------
* Adding a new permission code is safe.
* Adding a permission to an existing role is safe.
* Removing or renaming a code requires a data migration that first re-points
  every ``RoleBinding`` / ``role_permissions`` row that references it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PermissionDef:
    code: str
    name: str
    description: str


@dataclass(frozen=True)
class SystemRoleDef:
    code: str
    name: str
    description: str
    permissions: tuple[str, ...]


# ---------------------------------------------------------------------------
# Permission catalogue
# ---------------------------------------------------------------------------

PERMISSIONS: tuple[PermissionDef, ...] = (
    # Organisation-scoped
    PermissionDef(
        code="org:admin",
        name="Administer organisation",
        description="Full control of an organisation, including billing and members.",
    ),
    PermissionDef(
        code="org:read",
        name="View organisation",
        description="Read organisation metadata and settings.",
    ),
    PermissionDef(
        code="member:invite",
        name="Invite members",
        description="Invite new users into the organisation.",
    ),
    PermissionDef(
        code="member:manage",
        name="Manage members",
        description="Update member roles, disable or archive accounts.",
    ),
    PermissionDef(
        code="audit:read",
        name="Read audit log",
        description="View the append-only governance audit trail.",
    ),
    # Space-scoped
    PermissionDef(
        code="space:create",
        name="Create spaces",
        description="Create new collaborative or managed spaces.",
    ),
    PermissionDef(
        code="space:manage",
        name="Manage space",
        description="Update settings, members, retention, and governance for a space.",
    ),
    PermissionDef(
        code="space:write",
        name="Author in space",
        description="Create and edit knowledge, agents, and workflows in a space.",
    ),
    PermissionDef(
        code="space:read",
        name="Read space",
        description="Query knowledge and observe activity in a space.",
    ),
    PermissionDef(
        code="space:delete",
        name="Delete space",
        description="Archive or hard-delete a space.",
    ),
)


# Convenience lookup for consumers that want validation by code.
PERMISSION_CODES: frozenset[str] = frozenset(p.code for p in PERMISSIONS)


# ---------------------------------------------------------------------------
# System roles
# ---------------------------------------------------------------------------
# ``owner`` intentionally holds *every* permission — the first user of a new
# organisation gets this role so they can bootstrap everything else.

SYSTEM_ROLES: tuple[SystemRoleDef, ...] = (
    # ---- Organisation-scoped roles ----
    SystemRoleDef(
        code="owner",
        name="Owner",
        description="Full control across the organisation and all its spaces.",
        permissions=tuple(p.code for p in PERMISSIONS),
    ),
    SystemRoleDef(
        code="admin",
        name="Administrator",
        description="Manage members, spaces, and audit; cannot dissolve the org.",
        permissions=(
            "org:read",
            "member:invite",
            "member:manage",
            "audit:read",
            "space:create",
            "space:manage",
            "space:write",
            "space:read",
            "space:delete",
        ),
    ),
    SystemRoleDef(
        code="member",
        name="Member",
        description=(
            "Baseline organisation membership: read org metadata, own a "
            "personal space, and create collaborative spaces. Access to any "
            "given space is granted separately at the space level."
        ),
        permissions=(
            "org:read",
            "space:create",
        ),
    ),
    # ---- Space-scoped roles (bound to Space resources, never to Organization) ----
    SystemRoleDef(
        code="editor",
        name="Editor",
        description=(
            "Author knowledge, agents, and workflows inside a specific space. "
            "Only meaningful when bound to a Space resource."
        ),
        permissions=(
            "org:read",
            "space:write",
            "space:read",
        ),
    ),
    SystemRoleDef(
        code="viewer",
        name="Viewer",
        description=(
            "Read-only access to a specific space. Only meaningful when bound "
            "to a Space resource."
        ),
        permissions=(
            "org:read",
            "space:read",
        ),
    ),
)


SYSTEM_ROLE_CODES: frozenset[str] = frozenset(r.code for r in SYSTEM_ROLES)


def system_role(code: str) -> SystemRoleDef:
    """Return the ``SystemRoleDef`` matching ``code`` or raise ``KeyError``."""

    for role in SYSTEM_ROLES:
        if role.code == code:
            return role
    raise KeyError(f"unknown system role code: {code!r}")
