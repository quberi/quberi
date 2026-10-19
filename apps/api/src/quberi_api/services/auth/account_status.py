"""Who may sign in.

A member whose account is suspended (or archived), or whose organization is not
active, cannot sign in and cannot hold a web session. This module holds the
error, the message every sign-in path uses (it does not say why the account is
unavailable), and the helpers the sign-in paths share.

Which helper a sign-in path may use depends on what it can prove:

* Phone, Google and Feishu confirm *who the person is* (the provider vouches for
  the phone number, the Google subject or the Feishu open_id, and each is the key
  of one ``Identity``). They use :func:`best_login_user` and
  :func:`raise_if_identity_suspended`, which work per identity.
* An email code only proves control of *that address*. The email path never looks
  at the identity: it may only sign in a row whose own email is the verified one
  (see ``verification._find_active_user``). Do not call the two identity helpers
  from the email path, or a recycled work mailbox could reach the previous
  owner's other accounts.
"""

from __future__ import annotations

import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from quberi_api.domain.enums import BLOCKED_USER_STATUSES
from quberi_api.domain.identity.models import Organization, User
from quberi_api.services.errors import ServiceError

ACCOUNT_UNAVAILABLE_MESSAGE = (
    "此账号暂不可用,请联系组织管理员。 "
    "This account is unavailable. Contact your organisation administrator."
)


class AccountUnavailableError(ServiceError):
    """The account may not sign in right now (HTTP 401, code ``account_unavailable``)."""


def login_usable() -> ColumnElement[bool]:
    """SQL condition: the user row is in a state that may sign in."""

    return User.status.notin_(BLOCKED_USER_STATUSES)


async def best_login_user(
    session: AsyncSession, *, identity_id: uuid.UUID
) -> Optional[tuple[User, Organization]]:
    """The identity's best account that may sign in, or ``None``.

    Only for sign-in paths where a provider verified the identity (phone, Google,
    Feishu); never for the email path. Order: higher organization tier first, then
    the oldest account.
    """

    row = (
        await session.execute(
            select(User, Organization)
            .join(Organization, Organization.id == User.organization_id)
            .where(User.identity_id == identity_id)
            .where(login_usable())
            .order_by(Organization.tier.desc(), User.created_at.asc())
            .limit(1)
        )
    ).first()
    return (row[0], row[1]) if row else None


async def raise_if_identity_suspended(
    session: AsyncSession, *, identity_id: uuid.UUID
) -> None:
    """Refuse when the identity has a suspended account.

    Call it after :func:`best_login_user` found nothing and before the sign-in path
    would create or link an account, so a deactivated member is not given a new
    workspace. Same restriction as :func:`best_login_user`: provider-verified paths
    only.
    """

    suspended = (
        await session.execute(
            select(User.id)
            .where(User.identity_id == identity_id, User.status == "suspended")
            .limit(1)
        )
    ).first()
    if suspended is not None:
        raise AccountUnavailableError(ACCOUNT_UNAVAILABLE_MESSAGE)
