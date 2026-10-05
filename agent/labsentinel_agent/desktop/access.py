"""One place the agent asks about the desktop, whichever way it can be reached.

* As a Windows Service (session 0): through the desktop worker in the user's
  session (proxy.py). If no worker is connected, the answers are empty, which
  the safety gate treats as "leave everything alone", and the heartbeat tells
  the dashboard that desktop access is missing.
* From a console (development, ``labsentinel-agent`` in a user session): the
  local Windows APIs directly, exactly as before.
"""

from __future__ import annotations

from typing import Protocol


class _Proxy(Protocol):
    @property
    def connected(self) -> bool: ...
    def window_index(self) -> dict[int, str] | None: ...
    def foreground(self) -> tuple[str, str] | None: ...
    def capture_jpeg(self, *, max_width: int = ..., quality: int = ...) -> bytes | None: ...
    def close_windows(self, pid: int) -> bool | None: ...
    def lock(self) -> bool | None: ...
    def freeze(self, on: bool) -> bool | None: ...


class _Local(Protocol):
    def window_index(self) -> dict[int, str]: ...
    def foreground(self) -> tuple[str, str]: ...
    def capture_jpeg(self, *, max_width: int, quality: int) -> bytes | None: ...
    def close_windows(self, pid: int) -> bool: ...
    def lock(self) -> bool: ...
    def freeze(self, on: bool) -> bool: ...


class DesktopAccess:
    def __init__(self, *, proxy: _Proxy | None, local: _Local, service_mode: bool) -> None:
        self._proxy = proxy
        self._local = local
        self._service_mode = service_mode

    @property
    def via_worker(self) -> bool:
        return self._proxy is not None and self._proxy.connected

    def available(self, *, someone_logged_in: bool = True) -> bool:
        """Can the agent see the desktop right now? (reported in heartbeats)

        A service with nobody at the console has nothing to see, which is fine.
        """
        if not self._service_mode or not someone_logged_in:
            return True
        return self.via_worker

    def window_index(self) -> dict[int, str] | None:
        """None means "ask locally" (the platform layer falls back)."""
        if self.via_worker:
            assert self._proxy is not None
            return self._proxy.window_index() or {}
        return {} if self._service_mode else None

    def foreground(self) -> tuple[str, str]:
        if self.via_worker:
            assert self._proxy is not None
            return self._proxy.foreground() or ("", "")
        return ("", "") if self._service_mode else self._local.foreground()

    def capture_jpeg(self, *, max_width: int = 1280, quality: int = 60) -> bytes | None:
        if self.via_worker:
            assert self._proxy is not None
            return self._proxy.capture_jpeg(max_width=max_width, quality=quality)
        if self._service_mode:
            return None  # session 0 would only capture a black screen
        return self._local.capture_jpeg(max_width=max_width, quality=quality)

    def close_windows(self, pid: int) -> bool:
        if self.via_worker:
            assert self._proxy is not None
            return bool(self._proxy.close_windows(pid))
        return False if self._service_mode else self._local.close_windows(pid)

    def lock(self) -> bool:
        if self.via_worker:
            assert self._proxy is not None
            return bool(self._proxy.lock())
        return False if self._service_mode else self._local.lock()

    def freeze(self, on: bool) -> bool:
        if self.via_worker:
            assert self._proxy is not None
            return bool(self._proxy.freeze(on))
        return False if self._service_mode else self._local.freeze(on)


class DesktopCapturer:
    """ScreenshotCapturer port backed by :class:`DesktopAccess`."""

    def __init__(self, desktop: DesktopAccess) -> None:
        self._desktop = desktop

    def capture_jpeg(self, *, max_width: int = 1280, quality: int = 60) -> bytes | None:
        return self._desktop.capture_jpeg(max_width=max_width, quality=quality)
