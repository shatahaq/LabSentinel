"""Ports (interfaces) for platform-specific I/O used by enforcement.

Keeping these abstract lets the enforcement engine be tested with in-memory
fakes, and lets the Windows implementations (pywin32 / psutil / WMI / mss) be
swapped or stubbed on non-Windows dev machines.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from labsentinel_agent.enforce.identity import ProcessInfo

# Called by a ProcessSource whenever a new user process appears.
ProcessStartCallback = Callable[[ProcessInfo], Awaitable[None]]


class ProcessController(Protocol):
    """Closes/kills processes. Only ever called for EVALUABLE violations."""

    def is_running(self, pid: int) -> bool: ...

    async def graceful_close(self, pid: int, timeout_s: float) -> bool:
        """Request a clean close (e.g. WM_CLOSE). True if it exited in time."""
        ...

    async def force_kill(self, pid: int) -> bool:
        """Last resort hard kill. Returns True if the process is gone."""
        ...


class WarningPresenter(Protocol):
    """Shows a transparent, non-focus-stealing warning to the participant."""

    def warn_closing(self, process_name: str, seconds: int, reason: str) -> None: ...

    def notify(self, title: str, message: str) -> None: ...


class ScreenshotCapturer(Protocol):
    """Captures evidence screenshots. Returns JPEG bytes, or None on failure."""

    def capture_jpeg(self, *, max_width: int = 1280, quality: int = 60) -> bytes | None: ...


class ProcessSource(Protocol):
    """Supplies process start events and current process snapshots."""

    def list_processes(self) -> list[ProcessInfo]: ...

    def watch_new_processes(self, on_start: ProcessStartCallback) -> Awaitable[None]:
        """Begin delivering new-process events until cancelled."""
        ...
