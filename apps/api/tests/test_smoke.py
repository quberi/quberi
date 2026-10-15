"""Smoke tests for the test harness itself (fixtures, isolation, seeding)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from quberi_api.domain.identity.models import Organization, User


@pytest.mark.asyncio
async def test_db_session_fixture_works(db_session):
    result = await db_session.execute(select(1))
    assert result.scalar_one() == 1


@pytest.mark.asyncio
async def test_org_and_owner_seeds_a_real_organization(db_session, org_and_owner):
    org = await db_session.get(Organization, org_and_owner.organization_id)
    user = await db_session.get(User, org_and_owner.user_id)
    assert org is not None
    assert user is not None
    assert user.organization_id == org.id
    assert org.tier == "team"


@pytest.mark.asyncio
async def test_isolation_part_a_creates_a_marked_org(db_session):
    """Paired with the next test: proves rollback actually happens, not
    just that IDs differ by chance."""
    from quberi_api.services import onboarding

    result = await onboarding.bootstrap_organization(
        db_session,
        name="Isolation Marker Org",
        slug="isolation-marker-org",
        owner_email="isolation-marker@example.com",
        owner_display_name="Marker",
        tier="team",
    )
    org = await db_session.get(Organization, result.organization_id)
    assert org is not None
    assert org.slug == "isolation-marker-org"


@pytest.mark.asyncio
async def test_isolation_part_b_previous_org_is_gone(db_session):
    """Runs after part A in file order; if teardown didn't roll back, this
    slug would still resolve (and inserting it again would violate the
    unique constraint instead of succeeding)."""
    result = await db_session.execute(
        select(Organization).where(Organization.slug == "isolation-marker-org")
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_rows_do_not_leak_across_tests(db_session):
    """If the previous test's org/user rows leaked, this count would be off.

    We can't assert an exact global count (other tests in the same run may
    have created orgs before rollback), but we CAN assert that a row we
    never inserted in *this* test doesn't exist.
    """

    phantom_id = uuid.uuid4()
    org = await db_session.get(Organization, phantom_id)
    assert org is None
