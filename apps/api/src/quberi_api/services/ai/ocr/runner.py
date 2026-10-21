"""Runs the OCR worker in a child process and keeps it on a short leash.

The parent (API or indexing worker) never loads a model. It starts one child on first
use, sends it one picture at a time, and stops it when it has been idle for a while, so
an instance that rarely sees a picture holds none of the model memory.

Things that go wrong are turned into :class:`OcrError` with a plain reason, and the child
is thrown away so the next picture gets a fresh one:

* took too long: killed, :class:`OcrTimeoutError`
* died (out of memory, crash): :class:`OcrUnavailableError` saying which
* could not start (models missing, hash mismatch): :class:`OcrUnavailableError`
* picture is unreadable: :class:`OcrImageError` (the child is kept, nothing is wrong with it)
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import multiprocessing
import time
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import Any

from .base import OcrError, OcrImageError, OcrTimeoutError, OcrUnavailableError
from .worker import MEMORY_EXIT_CODE

_log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.1
_STOP_GRACE_S = 5.0


class ProcessRunner:
    """One child process, one picture at a time (concurrency 1 by design)."""

    def __init__(
        self,
        *,
        target: Callable[..., None],
        target_args: tuple[Any, ...] = (),
        max_memory_mb: int = 2048,
        request_timeout_s: float = 60.0,
        startup_timeout_s: float = 300.0,
        idle_unload_s: float = 300.0,
    ) -> None:
        self._target = target
        self._target_args = target_args
        self._max_memory_mb = max_memory_mb
        self._request_timeout_s = request_timeout_s
        self._startup_timeout_s = startup_timeout_s
        self._idle_unload_s = idle_unload_s
        # ``spawn`` (not ``fork``): a forked child would inherit the parent's database
        # connections, event loop and threads.
        self._ctx = multiprocessing.get_context("spawn")
        self._lock = asyncio.Lock()
        self._proc: Any = None
        self._conn: Connection | None = None
        self._ids = itertools.count(1)
        self._last_used = 0.0
        self._idle_handle: asyncio.TimerHandle | None = None
        self._closed = False

    # -- public -------------------------------------------------------------------
    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.is_alive()

    async def call(self, picture: bytes) -> dict[str, Any]:
        """Send one picture, wait for the answer. Raises :class:`OcrError`."""

        if self._closed:
            raise OcrUnavailableError("the OCR engine has been shut down")
        async with self._lock:
            self._cancel_idle_timer()
            loop = asyncio.get_running_loop()
            try:
                return await loop.run_in_executor(None, self._blocking_call, picture)
            finally:
                self._last_used = time.monotonic()
                self._schedule_idle_stop(loop)

    async def aclose(self) -> None:
        self._closed = True
        self._cancel_idle_timer()
        async with self._lock:
            await asyncio.get_running_loop().run_in_executor(None, self._stop, True)

    # -- idle unload --------------------------------------------------------------
    def _cancel_idle_timer(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def _schedule_idle_stop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._idle_unload_s <= 0 or self._closed:
            return
        self._idle_handle = loop.call_later(
            self._idle_unload_s, lambda: loop.create_task(self._stop_if_idle())
        )

    async def _stop_if_idle(self) -> None:
        async with self._lock:
            if time.monotonic() - self._last_used >= self._idle_unload_s - 0.05:
                _log.info("OCR engine idle for %.0fs; unloading it", self._idle_unload_s)
                await asyncio.get_running_loop().run_in_executor(None, self._stop, True)

    # -- blocking side (runs in a thread, one at a time) ---------------------------
    def _blocking_call(self, picture: bytes) -> dict[str, Any]:
        self._ensure_started()
        assert self._conn is not None
        request_id = next(self._ids)
        try:
            self._conn.send((request_id, picture))
        except (OSError, ValueError, BrokenPipeError) as exc:
            raise self._died("while sending the picture") from exc
        deadline = time.monotonic() + self._request_timeout_s
        while True:
            reply = self._wait_for_message(deadline, "reading the picture")
            if reply[0] == "ok" and reply[1] == request_id:
                return reply[2]
            if reply[0] == "err" and reply[1] == request_id:
                kind, message = reply[2], reply[3]
                if kind == "image":
                    raise OcrImageError(message)
                self._stop()
                raise OcrUnavailableError(f"the OCR engine failed: {message}")
            # A late answer to an earlier, abandoned request: ignore it.

    def _ensure_started(self) -> None:
        if self.running and self._conn is not None:
            return
        self._stop()
        parent, child = self._ctx.Pipe(duplex=True)
        proc = self._ctx.Process(
            target=self._target,
            args=(child, *self._target_args, self._max_memory_mb),
            daemon=True,
        )
        proc.start()
        child.close()
        self._proc, self._conn = proc, parent
        _log.info("starting the OCR engine (pid %s)", proc.pid)
        reply = self._wait_for_message(
            time.monotonic() + self._startup_timeout_s, "loading the models"
        )
        if reply[0] == "fatal":
            self._stop()
            raise OcrUnavailableError(f"the OCR engine could not start: {reply[1]}")
        if reply[0] != "ready":
            self._stop()
            raise OcrUnavailableError("the OCR engine sent an unexpected first message")

    def _wait_for_message(self, deadline: float, doing: str) -> tuple[Any, ...]:
        assert self._conn is not None and self._proc is not None
        while True:
            try:
                if self._conn.poll(_POLL_INTERVAL_S):
                    return self._conn.recv()
            except (EOFError, OSError) as exc:
                raise self._died(doing) from exc
            if not self._proc.is_alive():
                # It may have answered just before exiting.
                with contextlib.suppress(EOFError, OSError):
                    if self._conn.poll(0):
                        return self._conn.recv()
                raise self._died(doing)
            if time.monotonic() >= deadline:
                self._stop()
                raise OcrTimeoutError(f"the OCR engine took too long while {doing}")

    def _died(self, doing: str) -> OcrError:
        code = self._proc.exitcode if self._proc is not None else None
        if self._proc is not None:
            self._proc.join(timeout=1.0)
            code = self._proc.exitcode
        self._stop()
        if code == MEMORY_EXIT_CODE:
            return OcrUnavailableError(
                f"the OCR engine was stopped for using more than {self._max_memory_mb} MB "
                f"of memory while {doing}"
            )
        if code in (-9, 137):
            return OcrUnavailableError(
                f"the OCR engine was killed by the system (most likely out of memory) "
                f"while {doing}"
            )
        return OcrUnavailableError(
            f"the OCR engine stopped unexpectedly (exit {code}) while {doing}"
        )

    def _stop(self, graceful: bool = False) -> None:
        """Stop the child. By default it is killed at once (a stuck child will not listen);
        ``graceful`` asks it to exit first, for a normal shutdown or idle unload."""

        proc, conn = self._proc, self._conn
        self._proc, self._conn = None, None
        if graceful and conn is not None:
            with contextlib.suppress(Exception):
                conn.send(None)
            if proc is not None:
                with contextlib.suppress(Exception):
                    proc.join(timeout=_STOP_GRACE_S)
        if proc is not None and proc.is_alive():
            with contextlib.suppress(Exception):
                proc.kill()
                proc.join(timeout=_STOP_GRACE_S)
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
