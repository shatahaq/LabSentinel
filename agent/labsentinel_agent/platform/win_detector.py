"""Windows WMI event-based process-start detection (low latency).

Subscribes to ``Win32_ProcessStartTrace`` and emits a ProcessInfo for each new
process. WMI's watcher is blocking, so it runs in a worker thread and hands
events back to the asyncio loop. If WMI is unavailable (no admin / disabled),
the caller falls back to PollingProcessSource — this keeps detection working
without aggressive polling.

Requires administrative privileges for the trace subscription; verified on the
manual checklist.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading

from labsentinel_agent.enforce.identity import ProcessInfo
from labsentinel_agent.enforce.ports import ProcessStartCallback
from labsentinel_agent.platform.win_process import process_info_for

log = logging.getLogger("labsentinel.agent.wmi")

_IS_WINDOWS = sys.platform == "win32"


class WmiProcessStartSource:
    """Low-latency process-start events via WMI, bridged to asyncio."""

    def __init__(self, *, poll_ms: int = 500) -> None:
        self._poll_ms = poll_ms
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def stop(self) -> None:
        self._stop.set()

    def available(self) -> bool:
        if not _IS_WINDOWS:
            return False
        try:
            import pythoncom  # noqa: F401
            import wmi  # noqa: F401

            return True
        except ImportError:
            return False

    async def watch_new_processes(self, on_start: ProcessStartCallback) -> None:
        if not self.available():
            raise RuntimeError("WMI not available; use PollingProcessSource")

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[int] = asyncio.Queue()

        def _worker() -> None:
            import pythoncom
            import wmi

            pythoncom.CoInitialize()
            try:
                conn = wmi.WMI()
                watcher = conn.Win32_ProcessStartTrace.watch_for()
                while not self._stop.is_set():
                    try:
                        event = watcher(timeout_ms=self._poll_ms)
                    except wmi.x_wmi_timed_out:
                        continue
                    except Exception:
                        log.warning("WMI watcher error", exc_info=True)
                        break
                    try:
                        pid = int(event.ProcessID)
                    except (AttributeError, ValueError):
                        continue
                    loop.call_soon_threadsafe(queue.put_nowait, pid)
            finally:
                pythoncom.CoUninitialize()

        self._thread = threading.Thread(target=_worker, name="wmi-procwatch", daemon=True)
        self._thread.start()

        try:
            while not self._stop.is_set():
                try:
                    pid = await asyncio.wait_for(queue.get(), timeout=0.5)
                except TimeoutError:
                    continue
                info: ProcessInfo | None = await loop.run_in_executor(None, process_info_for, pid)
                if info is not None:
                    await on_start(info)
        finally:
            self._stop.set()
