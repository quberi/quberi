"""SMTP email delivery.

Uses the stdlib ``smtplib`` run in a worker thread (``asyncio.to_thread``)
so we don't pull in an async-SMTP dependency. This is the single swap-in
point for a transactional provider (Aliyun/Tencent/SES/Resend) or an SMS
sender later — callers only see :func:`send_email`.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
from dataclasses import dataclass
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Optional

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int
    username: Optional[str]
    password: Optional[str]  # plaintext (unsealed just-in-time)
    from_address: str
    use_ssl: bool
    from_name: str = "Quberi"
    name: str = "default"


class EmailSendError(RuntimeError):
    """Raised when an email could not be handed to the SMTP server."""


def _send_blocking(
    config: SmtpConfig, to_address: str, subject: str, body_text: str
) -> None:
    msg = MIMEText(body_text, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((config.from_name, config.from_address))
    msg["To"] = to_address

    if config.use_ssl:
        server: smtplib.SMTP = smtplib.SMTP_SSL(config.host, config.port, timeout=20)
    else:
        server = smtplib.SMTP(config.host, config.port, timeout=20)
    try:
        server.ehlo()
        if not config.use_ssl:
            try:
                server.starttls()
                server.ehlo()
            except smtplib.SMTPException:
                # Server without STARTTLS — proceed unencrypted only if the
                # operator explicitly chose a non-SSL port. Rare; log it.
                _log.warning("SMTP server %s did not support STARTTLS", config.host)
        if config.username and config.password:
            server.login(config.username, config.password)
        server.sendmail(config.from_address, [to_address], msg.as_string())
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 — quit failures are non-fatal
            pass


async def send_email(
    config: SmtpConfig, *, to_address: str, subject: str, body_text: str
) -> None:
    """Deliver one email; raises :class:`EmailSendError` on failure."""

    try:
        await asyncio.to_thread(
            _send_blocking, config, to_address, subject, body_text
        )
    except (smtplib.SMTPException, OSError) as exc:
        _log.warning("SMTP send to %s via %s failed: %s", to_address, config.host, exc)
        raise EmailSendError(f"{type(exc).__name__}: {exc}") from exc


async def send_email_with_failover(
    configs: list[SmtpConfig],
    *,
    to_address: str,
    subject: str,
    body_text: str,
) -> SmtpConfig:
    """Try each provider in order; return the one that delivered.

    Raises :class:`EmailSendError` only if *every* provider fails (or none
    are configured). This is what makes "163 primary, Gmail fallback"
    resilient: if the primary is unreachable (e.g. Gmail from a CN network),
    the next one is tried automatically.
    """

    if not configs:
        raise EmailSendError("no email providers configured")
    last_error: Optional[Exception] = None
    for config in configs:
        try:
            await send_email(
                config, to_address=to_address, subject=subject, body_text=body_text
            )
            _log.info("email to %s delivered via %s", to_address, config.name)
            return config
        except EmailSendError as exc:
            last_error = exc
            continue
    raise EmailSendError(
        f"all {len(configs)} email provider(s) failed; last: {last_error}"
    )
