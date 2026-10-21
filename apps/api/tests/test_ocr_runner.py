"""The OCR process runner: timeouts, crashes, memory kills, idle unload.

These start real child processes, but with stand-in workers that never import torch, so
they are quick. What they prove is the mechanics a bad picture or a bad day depends on.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from quberi_api.services.ai.ocr.base import (
    OcrImageError,
    OcrTimeoutError,
    OcrUnavailableError,
)
from quberi_api.services.ai.ocr.models import EngineConfig
from quberi_api.services.ai.ocr.runner import ProcessRunner
from quberi_api.services.ai.ocr.worker import MEMORY_EXIT_CODE
from quberi_api.services.ai.ocr.worker import main as real_worker_main


# -- stand-in engines (module level so the spawned child can import them) -------------
class EchoEngine:
    def run(self, picture: bytes):
        if picture == b"bad":
            raise OcrImageError("the file is not a readable picture")
        if picture == b"boom":
            raise ValueError("model exploded")
        if picture == b"slow":
            time.sleep(30)
        if picture == b"die":
            os._exit(1)
        if picture == b"eat":
            # Hold far more memory than the test limit allows and keep the loop alive
            # long enough for the watchdog to notice.
            hog = bytearray(400 * 1024 * 1024)
            hog[::4096] = b"x" * len(hog[::4096])
            time.sleep(10)
        return {"lines": [(picture.decode(), 0.9, ((0, 0), (1, 0), (1, 1), (0, 1)))],
                "width": 1, "height": 1, "dropped": 0, "notes": [], "pid": os.getpid()}


def echo_factory(_config):
    return EchoEngine()


def failing_factory(_config):
    raise RuntimeError("weights do not match the audited hash")


def echo_worker(conn, config, max_memory_mb):
    real_worker_main(conn, config, max_memory_mb, engine_factory=echo_factory)


def failing_worker(conn, config, max_memory_mb):
    real_worker_main(conn, config, max_memory_mb, engine_factory=failing_factory)


def _runner(target=echo_worker, **kw):
    kw.setdefault("request_timeout_s", 5.0)
    kw.setdefault("startup_timeout_s", 20.0)
    kw.setdefault("idle_unload_s", 0)
    kw.setdefault("max_memory_mb", 4096)
    return ProcessRunner(target=target, target_args=(EngineConfig(),), **kw)


@pytest.fixture
async def runner():
    made = []

    def make(**kw):
        r = _runner(**kw)
        made.append(r)
        return r

    yield make
    for r in made:
        await r.aclose()


async def test_round_trip_and_the_child_is_reused(runner):
    r = runner()
    first = await r.call(b"hello")
    second = await r.call(b"again")
    assert first["lines"][0][0] == "hello"
    assert first["pid"] == second["pid"] != os.getpid()


async def test_a_bad_picture_is_reported_and_the_child_survives(runner):
    r = runner()
    pid = (await r.call(b"ok"))["pid"]
    with pytest.raises(OcrImageError, match="not a readable picture"):
        await r.call(b"bad")
    assert (await r.call(b"ok"))["pid"] == pid


async def test_an_engine_crash_in_python_restarts_the_child(runner):
    r = runner()
    pid = (await r.call(b"ok"))["pid"]
    with pytest.raises(OcrUnavailableError, match="model exploded"):
        await r.call(b"boom")
    assert (await r.call(b"ok"))["pid"] != pid


async def test_a_stuck_child_is_killed_at_the_timeout_and_replaced(runner):
    r = runner(request_timeout_s=1.0)
    pid = (await r.call(b"ok"))["pid"]
    started = time.monotonic()
    with pytest.raises(OcrTimeoutError):
        await r.call(b"slow")
    assert time.monotonic() - started < 10  # killed, not waited out
    assert (await r.call(b"ok"))["pid"] != pid


async def test_a_dead_child_is_reported_and_replaced(runner):
    r = runner()
    pid = (await r.call(b"ok"))["pid"]
    with pytest.raises(OcrUnavailableError, match="exit 1"):
        await r.call(b"die")
    assert (await r.call(b"ok"))["pid"] != pid


async def test_a_child_over_the_memory_limit_is_stopped_and_the_reason_is_given(runner):
    # The test process itself imports plenty, so give the child a limit it will exceed
    # by holding a 400 MB buffer, but that an idle child stays under.
    r = runner(max_memory_mb=300, request_timeout_s=15.0)
    await r.call(b"ok")
    with pytest.raises(OcrUnavailableError, match="more than 300 MB"):
        await r.call(b"eat")
    assert (await r.call(b"ok"))["lines"][0][0] == "ok"


async def test_a_worker_that_cannot_start_says_why(runner):
    r = runner(target=failing_worker)
    with pytest.raises(OcrUnavailableError, match="audited hash"):
        await r.call(b"x")
    assert not r.running


async def test_the_child_is_stopped_when_idle_and_started_again_on_demand(runner):
    r = runner(idle_unload_s=0.6)
    pid = (await r.call(b"ok"))["pid"]
    assert r.running
    await asyncio.sleep(1.6)
    assert not r.running
    assert (await r.call(b"ok"))["pid"] != pid


async def test_requests_are_served_one_at_a_time(runner):
    r = runner()
    results = await asyncio.gather(*(r.call(f"n{i}".encode()) for i in range(5)))
    assert sorted(x["lines"][0][0] for x in results) == [f"n{i}" for i in range(5)]
    assert len({x["pid"] for x in results}) == 1


async def test_nothing_runs_after_close(runner):
    r = runner()
    await r.call(b"ok")
    await r.aclose()
    assert not r.running
    with pytest.raises(OcrUnavailableError, match="shut down"):
        await r.call(b"ok")


def test_the_memory_exit_code_is_distinct_from_common_failures():
    assert MEMORY_EXIT_CODE not in (0, 1, 2, 137, -9)
