"""Feishu (Lark) integration.

Public entry points:

* :class:`FeishuClient` — authenticated HTTP surface over the Feishu
  Open Platform. Handles ``tenant_access_token`` acquisition + Redis
  caching, unified error mapping, and retry on 429/5xx.
* :func:`get_platform_client` — shared client instance keyed on the
  platform-default credentials (``QUBERI_FEISHU_APP_ID`` /
  ``QUBERI_FEISHU_APP_SECRET`` in env). Per-org BYO credentials land
  when we add the ``integrations`` table.
"""

from quberi_api.services.integrations.feishu.client import (
    FeishuAuthError,
    FeishuClient,
    FeishuError,
    FeishuNotConfiguredError,
    get_platform_client,
)

__all__ = [
    "FeishuAuthError",
    "FeishuClient",
    "FeishuError",
    "FeishuNotConfiguredError",
    "get_platform_client",
]
