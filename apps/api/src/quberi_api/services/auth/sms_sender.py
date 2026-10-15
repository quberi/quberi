"""SMS delivery for phone verification codes.

No real provider is wired yet. When ``settings.sms_dev_log_only`` is true
(the default), :func:`send_sms` only logs the message so phone-code login
works locally without an SMS vendor. A concrete provider (Aliyun / Tencent
Cloud SMS, etc.) plugs in here later behind the same function signature,
mirroring the email ``send_email_with_failover`` design.
"""

from __future__ import annotations

import logging

from quberi_api.core.config import get_settings

_log = logging.getLogger(__name__)


class SmsSendError(Exception):
    """All configured SMS providers failed to deliver."""


async def send_sms(*, to_phone: str, text: str) -> None:
    """Send an SMS. Currently a dev stub that logs instead of sending.

    Raises :class:`SmsSendError` only once a real provider is configured
    and every attempt fails. In dev-log mode it never raises.
    """

    settings = get_settings()
    if settings.sms_dev_log_only:
        # Mirror the email DEV log so tests can read the code from logs.
        _log.warning("DEV sms to %s: %s", to_phone, text)
        return

    # No real provider implemented yet. Fail loudly so a misconfigured
    # production deploy doesn't silently drop login codes.
    raise SmsSendError(
        "no SMS provider configured (set sms_dev_log_only=true for local dev)"
    )
