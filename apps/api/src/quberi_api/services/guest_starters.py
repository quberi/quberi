"""Popularity of the suggested questions on the guest chat page.

When a guest asks one of the curated suggestions, today's counter for its key
is incremented. The page orders suggestions by the last
:data:`WINDOW_DAYS` days of counts and marks the most-asked ones as popular.

Only the curated key and a count are stored: no visitor-typed text, no IP, no
account. Free-form questions are deliberately not collected here (see the
feedback feature for an explicit, opt-in channel).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.domain.usage.models import GuestStarterStat
from quberi_api.services.demo_content import GUEST_STARTER_KEYS

# How far back popularity looks. Long enough to be stable, short enough that
# interest which has faded stops ranking.
WINDOW_DAYS = 30

# A suggestion is "popular" only if it is among the top few AND has really
# been asked a few times. The count floor stops one or two early clicks from
# crowning a "popular" question on a fresh install.
HOT_TOP_N = 3
HOT_MIN_COUNT = 3


@dataclass(frozen=True)
class StarterRank:
    key: str
    count: int
    hot: bool


def _today() -> date:
    return datetime.now(timezone.utc).date()


def is_valid_key(key: Optional[str]) -> bool:
    return key in GUEST_STARTER_KEYS


async def record(session: AsyncSession, key: str) -> None:
    """Count one guest asking suggestion ``key`` today. Unknown keys are ignored.

    Runs in its own short transaction (an atomic upsert), so concurrent guests
    can't lose increments and a failure never touches the caller's work.
    """

    if not is_valid_key(key):
        return
    # Release any read transaction opened earlier on this session.
    await session.commit()
    async with session.begin():
        stmt = pg_insert(GuestStarterStat).values(day=_today(), starter_key=key, count=1)
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[GuestStarterStat.day, GuestStarterStat.starter_key],
                set_={"count": GuestStarterStat.count + 1},
            )
        )


async def ranking(session: AsyncSession) -> list[StarterRank]:
    """Every suggestion, most-asked first.

    Ties (including the all-zero cold start) keep the curated order, so a new
    install shows the suggestions in the order they were written.
    """

    since = _today() - timedelta(days=WINDOW_DAYS - 1)
    rows = (
        await session.execute(
            select(GuestStarterStat.starter_key, func.sum(GuestStarterStat.count))
            .where(GuestStarterStat.day >= since)
            .group_by(GuestStarterStat.starter_key)
        )
    ).all()
    counts = {key: int(total or 0) for key, total in rows}
    curated = {key: i for i, key in enumerate(GUEST_STARTER_KEYS)}
    ordered = sorted(
        GUEST_STARTER_KEYS,
        key=lambda k: (-counts.get(k, 0), curated[k]),
    )
    return [
        StarterRank(
            key=k,
            count=counts.get(k, 0),
            hot=i < HOT_TOP_N and counts.get(k, 0) >= HOT_MIN_COUNT,
        )
        for i, k in enumerate(ordered)
    ]
