"""Platform email (SMTP) providers — DB-stored, env-bootstrapped, failover.

Multiple providers can be configured; the sender tries the ``enabled``
ones in ascending ``priority`` order until one delivers. On first use we
seed from env: the primary ``QUBERI_SMTP_*`` (priority 0) and, if present,
the secondary ``QUBERI_SMTP2_*`` (priority 1). Seeding is idempotent —
keyed by host — so it's safe to run every boot. Passwords are only ever
persisted Fernet-sealed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from quberi_api.core.config import get_settings
from quberi_api.domain.auth.models import PlatformEmailSettings
from quberi_api.services.auth.email_sender import SmtpConfig
from quberi_api.services.security import crypto

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderView:
    """Admin-facing view of one provider — never exposes the password."""

    id: str
    name: str
    priority: int
    enabled: bool
    host: str
    port: int
    username: Optional[str]
    from_address: str
    use_ssl: bool
    has_password: bool


def _to_view(row: PlatformEmailSettings) -> ProviderView:
    return ProviderView(
        id=str(row.id),
        name=row.name,
        priority=row.priority,
        enabled=row.enabled,
        host=row.host,
        port=row.port,
        username=row.username,
        from_address=row.from_address,
        use_ssl=row.use_ssl,
        has_password=bool(row.password_encrypted),
    )


def _to_smtp(row: PlatformEmailSettings) -> SmtpConfig:
    password = (
        crypto.unseal(row.password_encrypted) if row.password_encrypted else None
    )
    return SmtpConfig(
        host=row.host,
        port=row.port,
        username=row.username,
        password=password,
        from_address=row.from_address,
        use_ssl=row.use_ssl,
        name=row.name,
    )


async def _all_rows(session: AsyncSession) -> list[PlatformEmailSettings]:
    return list(
        (
            await session.execute(
                select(PlatformEmailSettings).order_by(
                    PlatformEmailSettings.priority.asc(),
                    PlatformEmailSettings.created_at.asc(),
                )
            )
        ).scalars().all()
    )


def _env_specs() -> list[dict]:
    """Provider specs from env (primary + optional secondary)."""

    s = get_settings()
    specs: list[dict] = []
    if s.smtp_host and s.smtp_from:
        specs.append(
            {
                "name": s.smtp_host,
                "priority": 0,
                "host": s.smtp_host,
                "port": s.smtp_port,
                "username": s.smtp_username,
                "password": (
                    s.smtp_password.get_secret_value() if s.smtp_password else None
                ),
                "from_address": s.smtp_from,
                "use_ssl": s.smtp_use_ssl,
            }
        )
    if s.smtp2_host and s.smtp2_from:
        specs.append(
            {
                "name": s.smtp2_host,
                "priority": 1,
                "host": s.smtp2_host,
                "port": s.smtp2_port,
                "username": s.smtp2_username,
                "password": (
                    s.smtp2_password.get_secret_value() if s.smtp2_password else None
                ),
                "from_address": s.smtp2_from,
                "use_ssl": s.smtp2_use_ssl,
            }
        )
    return specs


async def ensure_seeded(session: AsyncSession) -> None:
    """Seed any env-configured provider that isn't already present (by host)."""

    specs = _env_specs()
    if not specs:
        return
    rows = await _all_rows(session)
    existing_hosts = {r.host for r in rows}
    to_add = [spec for spec in specs if spec["host"] not in existing_hosts]
    if not to_add:
        return
    await session.commit()  # close the read txn before writing
    async with session.begin():
        # Re-check inside the txn.
        rows = await _all_rows(session)
        existing_hosts = {r.host for r in rows}
        for spec in specs:
            if spec["host"] in existing_hosts:
                continue
            session.add(
                PlatformEmailSettings(
                    name=spec["name"],
                    priority=spec["priority"],
                    enabled=True,
                    host=spec["host"],
                    port=spec["port"],
                    username=spec["username"],
                    password_encrypted=crypto.seal(spec["password"]),
                    from_address=spec["from_address"],
                    use_ssl=spec["use_ssl"],
                )
            )
            _log.info("seeded email provider %s (host=%s)", spec["name"], spec["host"])


async def list_providers(session: AsyncSession) -> list[ProviderView]:
    await ensure_seeded(session)
    return [_to_view(r) for r in await _all_rows(session)]


async def enabled_smtp_configs(session: AsyncSession) -> list[SmtpConfig]:
    """Enabled providers as ready-to-use SmtpConfigs, in failover order."""

    await ensure_seeded(session)
    rows = await _all_rows(session)
    return [_to_smtp(r) for r in rows if r.enabled and r.host and r.from_address]
