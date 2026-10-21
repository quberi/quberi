"""The child process: loads the OCR models once, then answers one picture at a time.

Why a child process at all: the models hold hundreds of megabytes, and an input that
makes the detector balloon (a 300 dpi scan once used ~43 GB in a published report) must
never take the API or the indexing worker down with it. Here the damage is limited to a
process that can be killed and restarted.

Protocol (over a ``multiprocessing`` pipe, plain tuples):

* child to parent, once: ``("ready",)`` or ``("fatal", message)``
* parent to child: ``(request_id, picture_bytes)`` or ``None`` to stop
* child to parent: ``("ok", request_id, result_dict)`` or
  ``("err", request_id, kind, message)`` with kind ``"image"`` or ``"engine"``
"""

from __future__ import annotations

import os
import resource
import sys
import threading
import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import Any

from .base import OcrImageError
from .models import EngineConfig

# Exit status of a child that stopped itself for using too much memory, so the parent can
# say so instead of reporting an anonymous crash.
MEMORY_EXIT_CODE = 86
_WATCH_INTERVAL_S = 0.2


def current_rss_mb() -> float:
    """Resident memory of this process in MB (current on Linux, peak elsewhere)."""

    try:
        with open("/proc/self/statm") as handle:
            pages = int(handle.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def _watch_memory(limit_mb: int) -> None:
    """Stop the process, hard, once it holds more than ``limit_mb``.

    This catches steady growth. A single huge allocation can still beat it between two
    checks, which is why the container's own memory limit stays in place as well.
    """

    while True:
        time.sleep(_WATCH_INTERVAL_S)
        if current_rss_mb() > limit_mb:
            os._exit(MEMORY_EXIT_CODE)


def main(
    conn: Connection,
    config: EngineConfig,
    max_memory_mb: int,
    engine_factory: Callable[[EngineConfig], Any] | None = None,
) -> None:
    """Entry point of the child. ``engine_factory`` exists so tests can run without torch."""

    if max_memory_mb > 0:
        threading.Thread(target=_watch_memory, args=(max_memory_mb,), daemon=True).start()
    try:
        if engine_factory is None:
            from .engine import OcrEngine

            engine_factory = OcrEngine.load
        engine = engine_factory(config)
    except Exception as exc:  # noqa: BLE001 — the parent needs the reason, whatever it is
        conn.send(("fatal", f"{type(exc).__name__}: {exc}"))
        return
    conn.send(("ready",))

    while True:
        try:
            message = conn.recv()
        except (EOFError, OSError):
            return
        if message is None:
            return
        request_id, picture = message
        try:
            conn.send(("ok", request_id, engine.run(picture)))
        except OcrImageError as exc:
            conn.send(("err", request_id, "image", str(exc)))
        except Exception as exc:  # noqa: BLE001
            conn.send(("err", request_id, "engine", f"{type(exc).__name__}: {exc}"))
