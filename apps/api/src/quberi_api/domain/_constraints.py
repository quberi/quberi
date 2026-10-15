"""Helpers for building consistent ``CheckConstraint`` values."""

from __future__ import annotations

from sqlalchemy import CheckConstraint


def in_values(column: str, values: tuple[str, ...], *, name: str) -> CheckConstraint:
    """Build a ``CheckConstraint`` restricting ``column`` to ``values``.

    ``name`` is only the *constraint suffix* used in the metadata naming
    convention (``ck_<table>_<name>``); pass a stable, descriptive suffix so
    Alembic diffs stay meaningful.
    """

    rendered = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({rendered})", name=name)
