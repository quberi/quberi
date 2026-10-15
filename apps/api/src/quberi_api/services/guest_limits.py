"""Daily caps for the guest demo assistant.

Anonymous visitors can ask the demo assistant questions, which spends the
platform's model budget. Two counters, both in Redis and keyed by UTC day so
they reset on their own:

* per client IP — keeps one visitor from using up the day's allowance;
* across all guests — the hard ceiling on spend. It still holds if someone
  spoofs their IP, which is why both exist.

Redis being unreachable **denies** the request: failing open would turn an
infrastructure hiccup into unbounded spend.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from redis.asyncio import Redis

from quberi_api.core.config import Settings

_log = logging.getLogger(__name__)

_KEY_TTL_SECONDS = 60 * 60 * 36  # a day plus slack; keys also roll over by date


class GuestLimitError(Exception):
    """A guest cap was hit (or the counters are unavailable)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason  # "ip" | "global" | "unavailable"
        self.message = message


@dataclass(frozen=True)
class GuestQuota:
    limit: int  # 0 = uncapped
    used: int

    @property
    def remaining(self) -> int:
        return -1 if self.limit == 0 else max(self.limit - self.used, 0)


def _day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ip_key(ip: str, kind: str = "ask") -> str:
    # "ask" keeps the original key names, so counts already in Redis carry over.
    if kind == "ask":
        return f"quberi:guest:ip:{_day()}:{ip}"
    return f"quberi:guest:{kind}:ip:{_day()}:{ip}"


def _global_key(kind: str = "ask") -> str:
    if kind == "ask":
        return f"quberi:guest:all:{_day()}"
    return f"quberi:guest:{kind}:all:{_day()}"


def _client(settings: Settings) -> Redis:
    return Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)


async def peek(settings: Settings, *, ip: str) -> GuestQuota:
    """Current per-IP position without consuming anything."""

    limit = settings.guest_daily_ip_limit
    if limit == 0:
        return GuestQuota(limit=0, used=0)
    client = _client(settings)
    try:
        raw: Optional[str] = await client.get(_ip_key(ip))
    except Exception as exc:  # noqa: BLE001
        _log.warning("guest quota peek failed: %s", exc)
        return GuestQuota(limit=limit, used=0)
    finally:
        await client.aclose()
    return GuestQuota(limit=limit, used=int(raw or 0))


async def consume(settings: Settings, *, ip: str) -> GuestQuota:
    """Count one guest question, or raise :class:`GuestLimitError`."""

    return await _consume(
        settings,
        ip=ip,
        kind="ask",
        ip_limit=settings.guest_daily_ip_limit,
        global_limit=settings.guest_daily_global_limit,
        ip_message=(
            "今天的免费提问次数用完了，登录后可以继续使用。"
            " You've used today's free questions — sign in to keep going."
        ),
        global_message=(
            "今天的访客体验名额已满，明天再来，或登录后继续使用。"
            " Today's guest demo is full — come back tomorrow, or sign in."
        ),
    )


async def consume_upload(settings: Settings, *, ip: str) -> GuestQuota:
    """Count one guest file upload, or raise :class:`GuestLimitError`."""

    return await _consume(
        settings,
        ip=ip,
        kind="upload",
        ip_limit=settings.guest_daily_upload_ip_limit,
        global_limit=settings.guest_daily_upload_global_limit,
        ip_message=(
            "今天的游客上传次数用完了，登录后可以继续上传。"
            " You've used today's guest uploads — sign in to upload more."
        ),
        global_message=(
            "今天的游客上传名额已满，明天再来，或登录后上传。"
            " Today's guest uploads are full — come back tomorrow, or sign in."
        ),
    )


async def _consume(
    settings: Settings,
    *,
    ip: str,
    kind: str,
    ip_limit: int,
    global_limit: int,
    ip_message: str,
    global_message: str,
) -> GuestQuota:
    """Shared counter logic for every kind of guest allowance.

    The IP counter is checked first and, when it is exhausted, does **not**
    touch the global counter - one noisy visitor can't burn the allowance
    everyone else shares. If Redis is unreachable the request is denied
    (fail closed), never let through.
    """

    client = _client(settings)
    try:
        used = 0
        if ip_limit:
            key = _ip_key(ip, kind)
            used = int(await client.incr(key))
            if used == 1:
                await client.expire(key, _KEY_TTL_SECONDS)
            if used > ip_limit:
                raise GuestLimitError("ip", ip_message)
        if global_limit:
            gkey = _global_key(kind)
            total = int(await client.incr(gkey))
            if total == 1:
                await client.expire(gkey, _KEY_TTL_SECONDS)
            if total > global_limit:
                raise GuestLimitError("global", global_message)
        return GuestQuota(limit=ip_limit, used=used)
    except GuestLimitError:
        raise
    except Exception as exc:  # noqa: BLE001 - fail closed
        _log.warning("guest %s quota unavailable: %s", kind, exc)
        raise GuestLimitError(
            "unavailable",
            "访客体验暂时不可用，请稍后再试。 The guest demo is temporarily unavailable.",
        ) from exc
    finally:
        await client.aclose()
