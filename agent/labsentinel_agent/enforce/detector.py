"""Process-start detection, transport/OS-agnostic.

``PollingProcessSource`` detects new processes by diffing snapshots on an
interval. It is the dependable fallback and is fully unit-testable with a fake
snapshot function (no OS calls). The Windows WMI event source
(platform/win_detector.py) is the low-latency primary; both feed the same
``on_start`` callback, so the engine doesn't care which produced an event.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from labsentinel_agent.enforce.identity import ProcessInfo
from labsentinel_agent.enforce.ports import ProcessStartCallback

log = logging.getLogger("labsentinel.agent.detector")

Snapshot = Callable[[], list[ProcessInfo]]
Sleep = Callable[[float], Awaitable[None]]


class PollingProcessSource:
    """Detects new PIDs by diffing periodic snapshots."""

    def __init__(
        self,
        snapshot: Snapshot,
        *,
        interval_s: float = 3.0,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._snapshot = snapshot
        self._interval = interval_s
        self._sleep = sleep
        self._stop = False

    def list_processes(self) -> list[ProcessInfo]:
        return self._snapshot()

    def stop(self) -> None:
        self._stop = True

    async def watch_new_processes(self, on_start: ProcessStartCallback) -> None:
        # Prime with processes already running, so we only report *new* ones.
        seen: set[int] = {p.pid for p in self._safe_snapshot()}
        while not self._stop:
            await self._sleep(self._interval)
            if self._stop:
                break
            current = self._safe_snapshot()
            current_pids = {p.pid for p in current}
            for proc in current:
                if proc.pid not in seen:
                    await self._safe_notify(on_start, proc)
            seen = current_pids

    def _safe_snapshot(self) -> list[ProcessInfo]:
        try:
            return self._snapshot()
        except Exception:
            log.warning("process snapshot failed", exc_info=True)
            return []

    @staticmethod
    async def _safe_notify(on_start: ProcessStartCallback, proc: ProcessInfo) -> None:
        try:
            await on_start(proc)
        except Exception:
            log.warning("on_start handler failed for pid=%d", proc.pid, exc_info=True)
