"""Freeze the participant's keyboard and mouse, with a full-screen notice.

A supervisor uses this from the dashboard on a PC caught cheating, to hold it
still while they walk over. It runs inside the desktop worker, which the
service starts as SYSTEM in the participant's own session, so it has the rights
to both ``BlockInput`` and a top-most window on that desktop.

Transparency, by design: freezing is never silent. A full-screen notice tells
the participant their PC was frozen by the supervisor, so this cannot be used
to watch without their knowledge.

Fail-safe, by design: Windows releases ``BlockInput`` automatically when the
calling process exits, and a participant can always break it with
Ctrl+Alt+Del. So a crashed agent, a killed worker or the secure-attention key
all return control to the participant -- freezing can never strand them.

Everything here is Windows-only and exercised by hand (see
docs/manual-test-checklist.md); off Windows the calls are no-ops.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading

log = logging.getLogger("labsentinel.agent.freeze")

_IS_WINDOWS = sys.platform == "win32"

# BlockInput can be quietly released (e.g. the secure-attention sequence), so
# the holder re-asserts it on this cadence while a freeze is in effect.
_REASSERT_EVERY_S = 0.2

_DEFAULT_NOTICE = (
    "Komputer ini dibekukan sementara oleh pengawas.\n"
    "Keyboard dan mouse akan aktif kembali setelah pengawas melepaskannya."
)


class _Freezer:
    """Holds the one active freeze (a worker never needs two at once)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def set(self, on: bool, notice: str) -> bool:
        if not _IS_WINDOWS:
            return False
        with self._lock:
            if on:
                if self._frozen:
                    return True
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._hold, args=(notice,), name="ls-freeze", daemon=True
                )
                self._thread.start()
                self._frozen = True
                return True
            # Release.
            if not self._frozen:
                return True
            self._stop.set()
            thread = self._thread
            if thread is not None:
                thread.join(timeout=3.0)
            self._thread = None
            self._frozen = False
            return True

    def _hold(self, notice: str) -> None:  # pragma: no cover - Windows, has a UI
        """Own the block on a dedicated thread with its own notice window.

        ``BlockInput`` blocks every thread except the one that called it, so the
        notice window this thread pumps keeps painting while the participant's
        input is held.
        """
        # windll exists only on Windows: the ignore is needed elsewhere only.
        user32 = ctypes.windll.user32  # type: ignore[attr-defined, unused-ignore]
        window = None
        try:
            window = _NoticeWindow(notice)
        except Exception:
            log.warning("freeze notice window failed; blocking input only", exc_info=True)
        try:
            user32.BlockInput(True)
            while not self._stop.is_set():
                if window is not None:
                    window.pump()
                # Re-assert: a released block (e.g. after Ctrl+Alt+Del) is retaken.
                user32.BlockInput(True)
                self._stop.wait(_REASSERT_EVERY_S)
        except Exception:
            log.warning("freeze loop failed", exc_info=True)
        finally:
            try:
                user32.BlockInput(False)
            except Exception:
                log.debug("BlockInput release failed", exc_info=True)
            if window is not None:
                window.destroy()


class _NoticeWindow:  # pragma: no cover - Windows, draws on the real desktop
    """A black, top-most, full-screen window that paints the freeze notice."""

    _CLASS = "LabSentinelFreezeNotice"
    _registered = False

    def __init__(self, text: str) -> None:
        import win32api
        import win32con
        import win32gui

        self._win32con = win32con
        self._win32gui = win32gui
        self._text = text

        if not _NoticeWindow._registered:
            wc = win32gui.WNDCLASS()
            wc.lpszClassName = self._CLASS
            wc.lpfnWndProc = {win32con.WM_PAINT: self._on_paint}
            wc.hbrBackground = win32gui.GetStockObject(win32con.BLACK_BRUSH)
            wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
            with_ = win32gui  # registration is process-wide, once
            with_.RegisterClass(wc)
            _NoticeWindow._registered = True

        vw = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
        vh = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
        vx = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
        vy = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
        style = win32con.WS_POPUP
        ex = win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW
        self._hwnd = win32gui.CreateWindowEx(
            ex, self._CLASS, "Lab Sentinel", style, vx, vy, vw, vh, 0, 0, 0, None
        )
        win32gui.ShowWindow(self._hwnd, win32con.SW_SHOW)
        win32gui.SetWindowPos(
            self._hwnd, win32con.HWND_TOPMOST, vx, vy, vw, vh,
            win32con.SWP_SHOWWINDOW,
        )

    def _on_paint(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        win32gui, win32con = self._win32gui, self._win32con
        hdc, ps = win32gui.BeginPaint(hwnd)
        try:
            left, top, right, bottom = win32gui.GetClientRect(hwnd)
            win32gui.SetTextColor(hdc, 0x00FFFFFF)  # white
            win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
            flags = win32con.DT_CENTER | win32con.DT_VCENTER | win32con.DT_WORDBREAK
            win32gui.DrawText(hdc, self._text, -1, (left, top, right, bottom), flags)
        finally:
            win32gui.EndPaint(hwnd, ps)
        return 0

    def pump(self) -> None:
        self._win32gui.PumpWaitingMessages()
        # Stay on top even if another window tries to cover the notice.
        self._win32gui.SetWindowPos(
            self._hwnd, self._win32con.HWND_TOPMOST, 0, 0, 0, 0,
            self._win32con.SWP_NOMOVE | self._win32con.SWP_NOSIZE | self._win32con.SWP_NOACTIVATE,
        )

    def destroy(self) -> None:
        try:
            self._win32gui.DestroyWindow(self._hwnd)
        except Exception:
            log.debug("freeze notice destroy failed", exc_info=True)


_freezer = _Freezer()


def set_input_frozen(on: bool, *, notice: str = _DEFAULT_NOTICE) -> bool:
    """Freeze (``on``) or release the keyboard and mouse. Windows only."""
    return _freezer.set(on, notice or _DEFAULT_NOTICE)


def input_is_frozen() -> bool:
    return _freezer.frozen
