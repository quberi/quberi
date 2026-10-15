"""Free-quota metering + admission for platform-shared model usage.

Policy
------
* An org that brought its own key (BYOK) is **never metered** — it pays its
  provider directly. We detect this by checking whether the org's chat
  selection resolves to a non-platform binding.
* An org on the **platform-shared** model gets a free monthly message
  allowance based on its tier (see ``settings.free_message_quota_*``).
  ``0`` means unlimited for that tier (e.g. team/enterprise by default).
* The counter is per ``(org, month)``. A new month starts a fresh row, so
  the reset is implicit — no scheduled job.

The admission call :func:`check_and_reserve` is atomic: it increments the
month's counter and rejects (raising :class:`FreeQuotaExhaustedError`) when
the increment would exceed the allowance. Callers invoke it once per user
turn, *before* the model call, only when the org is on the platform key.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.core.config import get_settings
from quberi_api.domain.ai.models import AiModelSelection, AiProviderBinding
from quberi_api.domain.usage.models import UsageCounter
from quberi_api.services.errors import ServiceError


class FreeQuotaExhaustedError(ServiceError):
    """The org's free monthly allowance for the shared model is used up."""


@dataclass(frozen=True)
class UsageStatus:
    """Snapshot of an org's free-quota position for the current month."""

    metered: bool  # False when BYOK (no limit applies)
    limit: int  # 0 = unlimited
    used: int
    remaining: int  # -1 when unlimited


def _period_start(now: datetime | None = None) -> date:
    d = (now or datetime.now(timezone.utc)).date()
    return d.replace(day=1)


def _quota_for_tier(tier: str) -> int:
    settings = get_settings()
    if tier == "personal":
        return settings.free_message_quota_personal
    # team / enterprise / anything else
    return settings.free_message_quota_team


async def chat_uses_platform_default(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> bool:
    """Would a chat call for this org resolve to the platform-shared key?

    Mirrors :func:`services.ai.resolver._resolve` for ``purpose='chat'``
    without instantiating a provider:

    * explicit selection → follow its binding's ``is_platform_default``.
    * no selection → falls back to the platform default → ``True``.
    """

    selection = (
        await session.execute(
            select(AiModelSelection).where(
                AiModelSelection.organization_id == organization_id,
                AiModelSelection.purpose == "chat",
            )
        )
    ).scalar_one_or_none()

    if selection is None:
        return True

    binding = (
        await session.execute(
            select(AiProviderBinding).where(
                AiProviderBinding.id == selection.provider_binding_id
            )
        )
    ).scalar_one_or_none()
    if binding is None:
        # Dangling selection → resolver would error; treat as platform for
        # safety so we don't hand out free unmetered calls.
        return True
    return bool(binding.is_platform_default)


async def get_status(
    session: AsyncSession, *, organization_id: uuid.UUID, tier: str
) -> UsageStatus:
    """Read-only quota snapshot for the current month (no mutation)."""

    on_platform = await chat_uses_platform_default(
        session, organization_id=organization_id
    )
    if not on_platform:
        return UsageStatus(metered=False, limit=0, used=0, remaining=-1)

    limit = _quota_for_tier(tier)
    used = (
        await session.execute(
            select(UsageCounter.chat_messages_used).where(
                UsageCounter.organization_id == organization_id,
                UsageCounter.period_start == _period_start(),
            )
        )
    ).scalar_one_or_none() or 0

    if limit == 0:  # unlimited
        return UsageStatus(metered=True, limit=0, used=used, remaining=-1)
    return UsageStatus(
        metered=True,
        limit=limit,
        used=used,
        remaining=max(0, limit - used),
    )


async def check_and_reserve(
    session: AsyncSession, *, organization_id: uuid.UUID, tier: str
) -> None:
    """Reserve one message against the month's free allowance.

    Raises :class:`FreeQuotaExhaustedError` when the allowance is spent.
    A no-op for unlimited tiers (``limit == 0``). Caller must have already
    confirmed the org is on the platform-shared key.
    """

    limit = _quota_for_tier(tier)
    period = _period_start()

    # Upsert the row, atomically incrementing the counter, and read back the
    # new value. We enforce the limit *after* the increment and roll back by
    # raising if it was exceeded — the surrounding request transaction unwinds
    # the increment, so a rejected call never consumes quota.
    stmt = (
        pg_insert(UsageCounter)
        .values(
            id=uuid.uuid4(),
            organization_id=organization_id,
            period_start=period,
            chat_messages_used=1,
        )
        .on_conflict_do_update(
            constraint="uq_usage_counters_org_period",
            set_={
                "chat_messages_used": UsageCounter.chat_messages_used + 1,
            },
        )
        .returning(UsageCounter.chat_messages_used)
    )
    new_used = (await session.execute(stmt)).scalar_one()

    if limit != 0 and new_used > limit:
        # Exceeded — undo the increment by failing the transaction.
        await session.rollback()
        raise FreeQuotaExhaustedError(
            f"free monthly allowance of {limit} messages is used up"
        )
    await session.commit()


async def enforce_free_quota(
    session: AsyncSession, *, organization_id: uuid.UUID, tier: str
) -> None:
    """Gate one chat turn: reserve quota only when on the platform key.

    BYOK orgs pass through untouched. Raises
    :class:`FreeQuotaExhaustedError` when a platform-key org is out of its
    free monthly allowance. Call once per user turn, before the model call.
    """

    if await chat_uses_platform_default(session, organization_id=organization_id):
        await check_and_reserve(
            session, organization_id=organization_id, tier=tier
        )
