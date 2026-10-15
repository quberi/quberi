"""Quberi domain models.

Importing this package pulls every SQLAlchemy model into scope so that
``Base.metadata`` is fully populated for Alembic autogeneration and for any
schema-inspection tooling.
"""

from quberi_api.domain import (  # noqa: F401
    agents,
    ai,
    audit,
    auth,
    authz,
    conversations,
    documents,
    identity,
    integrations,
    oauth,
    org,
    positions,
    spaces,
    usage,
)
