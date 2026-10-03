"""PollingProcessSource tests (fake snapshots, no OS calls)."""

from __future__ import annotations

import asyncio

import pytest

from labsentinel_agent.enforce.detector import PollingProcessSource
from labsentinel_agent.enforce.identity import ProcessInfo


def _p(pid: int, name: str = "x.exe") -> ProcessInfo:
    return ProcessInfo(pid=pid, name=name)


@pytest.mark.asyncio
async def test_reports_only_new_processes() -> None:
    snapshots = [
        [_p(1), _p(2)],  # initial (primed, not reported)
        [_p(1), _p(2), _p(3)],  # 3 is new -> reported
        [_p(1), _p(3), _p(4)],  # 4 is new -> reported (2 gone, not reported)
    ]
    calls = iter(snapshots)
    started: list[int] = []

    def snapshot() -> list[ProcessInfo]:
        try:
            return next(calls)
        except StopIteration:
            return snapshots[-1]

    async def no_sleep(_: float) -> None:
        return None

    src = PollingProcessSource(snapshot, interval_s=0, sleep=no_sleep)

    async def on_start(p: ProcessInfo) -> None:
        started.append(p.pid)
        if len(started) >= 2:
            src.stop()

    await asyncio.wait_for(src.watch_new_processes(on_start), timeout=2)
    assert started == [3, 4]


@pytest.mark.asyncio
async def test_snapshot_exception_does_not_crash_loop() -> None:
    state = {"n": 0}
    started: list[int] = []

    def snapshot() -> list[ProcessInfo]:
        state["n"] += 1
        if state["n"] == 2:
            raise OSError("boom")
        if state["n"] == 3:
            return [_p(99)]
        return []

    async def no_sleep(_: float) -> None:
        return None

    src = PollingProcessSource(snapshot, interval_s=0, sleep=no_sleep)

    async def on_start(p: ProcessInfo) -> None:
        started.append(p.pid)
        src.stop()

    await asyncio.wait_for(src.watch_new_processes(on_start), timeout=2)
    assert started == [99]  # survived the exception tick


@pytest.mark.asyncio
async def test_on_start_exception_isolated() -> None:
    started: list[int] = []
    snaps = iter([[], [_p(5)], [_p(5), _p(6)]])

    def snapshot() -> list[ProcessInfo]:
        try:
            return next(snaps)
        except StopIteration:
            return [_p(5), _p(6)]

    async def no_sleep(_: float) -> None:
        return None

    src = PollingProcessSource(snapshot, interval_s=0, sleep=no_sleep)

    async def on_start(p: ProcessInfo) -> None:
        started.append(p.pid)
        if p.pid == 5:
            raise RuntimeError("handler blew up")
        if p.pid == 6:
            src.stop()

    await asyncio.wait_for(src.watch_new_processes(on_start), timeout=2)
    assert started == [5, 6]  # handler error on 5 didn't stop detection of 6
