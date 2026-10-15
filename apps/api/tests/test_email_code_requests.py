"""Requesting an email code must work for the Feishu bind and account bind flows.

Both used to pass a keyword (``require_existing``) that ``request_code`` no
longer accepts, so every attempt ended in a 500 - for a Feishu sign-in that
meant a new user could never finish binding an email. These tests call the two
service functions end to end (no SMTP is configured in tests, so nothing is
sent) and check a code row is actually stored.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from quberi_api.domain.auth.models import VerificationCode
from quberi_api.services import account as account_service
from quberi_api.services.auth import feishu_login


async def _codes(db, email, purpose):
    return (
        await db.execute(
            select(func.count())
            .select_from(VerificationCode)
            .where(
                func.lower(VerificationCode.email) == email,
                VerificationCode.purpose == purpose,
            )
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_feishu_bind_email_code_is_requested(db_session):
    email = f"bind-{uuid.uuid4().hex[:8]}@example.com"
    ticket = feishu_login._seal_bind_ticket(subject="ou_test", name="Tester", email=None)
    await feishu_login.request_email_code_for_bind(
        db_session, ticket=ticket, email=email, ip_address="203.0.113.9"
    )
    assert await _codes(db_session, email, feishu_login._PURPOSE_BIND) == 1


@pytest.mark.asyncio
async def test_feishu_bind_email_code_rejects_a_bad_ticket(db_session):
    with pytest.raises(feishu_login.FeishuLoginError):
        await feishu_login.request_email_code_for_bind(
            db_session, ticket="not-a-ticket", email="x@example.com"
        )


@pytest.mark.asyncio
async def test_account_email_bind_code_is_requested(db_session, org_and_owner):
    email = f"new-{uuid.uuid4().hex[:8]}@example.com"
    await db_session.commit()
    await account_service.request_email_bind(
        db_session, identity_id=org_and_owner.identity_id, new_email=email
    )
    assert await _codes(db_session, email, account_service._BIND_PURPOSE) == 1
