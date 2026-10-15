"""Reusable FastAPI dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.infrastructure.registry import Infrastructure


def get_infrastructure(request: Request) -> Infrastructure:
    """Return the singleton ``Infrastructure`` bound to the running app."""

    return request.app.state.infrastructure  # type: ignore[no-any-return]


async def get_db_session(
    infrastructure: Infrastructure = Depends(get_infrastructure),
) -> AsyncIterator[AsyncSession]:
    """Yield a request-scoped SQLAlchemy session.

    We do **not** wrap the yield in ``session.begin()`` here. Services own
    their own transaction boundaries — that lets a single request compose
    several service calls into one atomic unit when needed.
    """

    async with infrastructure.session_factory() as session:
        yield session
