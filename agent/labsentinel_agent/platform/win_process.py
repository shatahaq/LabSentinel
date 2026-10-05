"""Windows process enumeration, window mapping, and process control.

Builds ``ProcessInfo`` snapshots (pid, name, path, owner, session, visible
window + title) from psutil + pywin32, and implements graceful close via
``WM_CLOSE`` with a hard-kill fallback. The visible-window map is what lets the
safety gate restrict enforcement to user-facing apps.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable

import psutil

from labsentinel_agent.enforce.identity import SYSTEM_ACCOUNTS, ProcessInfo

log = logging.getLogger("labsentinel.agent.platform")

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import win32api
    import win32con
    import win32gui
    import win32process
    import win32security
    import win32ts

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_CLASS = 20  # TOKEN_INFORMATION_CLASS.TokenElevation


def _active_console_session() -> int:
    if not _IS_WINDOWS:
        return -1
    try:
        return int(win32ts.WTSGetActiveConsoleSessionId())
    except Exception:
        return -1


def _session_of(pid: int) -> int | None:
    if not _IS_WINDOWS:
        return None
    try:
        return int(win32ts.ProcessIdToSessionId(pid))
    except Exception:
        return None


def is_elevated(pid: int) -> bool:
    """True when ``pid`` runs with administrator rights (an elevated token).

    False when it cannot be told (process gone, access denied): the other
    checks still decide, so an unknown never protects a participant's app.
    """
    if not _IS_WINDOWS:
        return False
    try:
        handle = win32api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        try:
            token = win32security.OpenProcessToken(handle, TOKEN_QUERY)
            try:
                return bool(win32security.GetTokenInformation(token, TOKEN_ELEVATION_CLASS))
            finally:
                win32api.CloseHandle(token)
        finally:
            win32api.CloseHandle(handle)
    except Exception:
        return False


# When the agent runs as a service (session 0), it cannot see the user's
# windows itself; the desktop worker in the user's session supplies them.
WindowIndexProvider = Callable[[], "dict[int, str] | None"]
_window_index_provider: WindowIndexProvider | None = None


def set_window_index_provider(provider: WindowIndexProvider | None) -> None:
    global _window_index_provider
    _window_index_provider = provider


def build_window_index() -> dict[int, str]:
    """Map pid -> a representative visible window title (one per pid).

    Asks the desktop worker when one is connected (service mode), otherwise
    enumerates this session's windows directly (console / development).
    """
    provider = _window_index_provider
    if provider is not None:
        supplied = provider()
        if supplied is not None:
            return supplied
    return local_window_index()


def current_session_id() -> int:
    """The Windows session this process runs in (-1 off Windows)."""
    session = _session_of(os.getpid())
    return -1 if session is None else session


def local_window_index() -> dict[int, str]:
    """Visible top-level windows of *this* session, via EnumWindows."""
    if not _IS_WINDOWS:
        return {}
    index: dict[int, str] = {}

    def _cb(hwnd: int, _: object) -> bool:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            index.setdefault(pid, title)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        log.debug("EnumWindows failed", exc_info=True)
    return index


def collect_processes(*, own_pids: frozenset[int] = frozenset()) -> list[ProcessInfo]:
    """Snapshot all processes with the fields the safety gate needs."""
    window_index = build_window_index()
    console = _active_console_session()
    out: list[ProcessInfo] = []
    for proc in psutil.process_iter(["pid", "name", "exe", "username"]):
        try:
            info = proc.info
            pid = info["pid"]
            name = (info.get("name") or "").lower()
            username = info.get("username")
            session = _session_of(pid)
            out.append(
                _make_info(pid, name, info.get("exe"), username, session, window_index, console)
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return out


def process_info_for(pid: int, *, console: int | None = None) -> ProcessInfo | None:
    """Build a ProcessInfo for a single pid (used on a start event)."""
    try:
        proc = psutil.Process(pid)
        with proc.oneshot():
            name = (proc.name() or "").lower()
            try:
                exe = proc.exe()
            except (psutil.AccessDenied, FileNotFoundError):
                exe = None
            try:
                username = proc.username()
            except psutil.AccessDenied:
                username = None
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    session = _session_of(pid)
    con = console if console is not None else _active_console_session()
    return _make_info(pid, name, exe, username, session, build_window_index(), con)


def _make_info(
    pid: int,
    name: str,
    exe: str | None,
    username: str | None,
    session: int | None,
    window_index: dict[int, str],
    console: int,
) -> ProcessInfo:
    is_system = (username or "").strip().lower() in SYSTEM_ACCOUNTS
    # Interactive user == owns a process in the active console session and is
    # not a system account.
    is_current = (
        console >= 0 and session is not None and session == console and not is_system
    )
    return ProcessInfo(
        pid=pid,
        name=name,
        exe_path=exe,
        username=username,
        session_id=session,
        has_visible_window=pid in window_index,
        window_title=window_index.get(pid, ""),
        is_current_user=is_current,
        # Only asked for the participant's own processes: the rest are out of
        # scope anyway, and this saves a token query per system process.
        elevated=is_current and is_elevated(pid),
    )


def post_wm_close(pid: int) -> bool:
    """Ask every visible window of ``pid`` in this session to close.

    Returns True if at least one window was asked. Only meaningful inside the
    user's session: from a service in session 0 there are no such windows,
    which is why the desktop worker does this on the service's behalf.
    """
    if not _IS_WINDOWS:
        return False
    posted = 0

    def _cb(hwnd: int, _: object) -> bool:
        nonlocal posted
        try:
            _, wpid = win32process.GetWindowThreadProcessId(hwnd)
            if wpid == pid and win32gui.IsWindowVisible(hwnd):
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                posted += 1
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        log.debug("EnumWindows during close failed", exc_info=True)
    return posted > 0


class WinProcessController:
    """Graceful close via WM_CLOSE, hard kill via psutil as a fallback.

    ``window_closer`` delivers the WM_CLOSE: by default in this session, or
    through the desktop worker when the agent runs as a service.
    """

    def __init__(self, window_closer: Callable[[int], bool | None] | None = None) -> None:
        self._close_windows = window_closer or post_wm_close

    def is_running(self, pid: int) -> bool:
        return bool(psutil.pid_exists(pid))

    async def graceful_close(self, pid: int, timeout_s: float) -> bool:
        if _IS_WINDOWS:
            await asyncio.to_thread(self._close_windows, pid)
        else:  # dev fallback: ask psutil to terminate
            await self._terminate(pid)
        return await self._wait_gone(pid, timeout_s)

    async def force_kill(self, pid: int) -> bool:
        try:
            p = psutil.Process(pid)
            p.kill()
        except psutil.NoSuchProcess:
            return True
        except psutil.AccessDenied:
            log.error("access denied killing pid=%d", pid)
            return False
        return await self._wait_gone(pid, 2.0)

    async def _terminate(self, pid: int) -> None:
        try:
            psutil.Process(pid).terminate()
        except psutil.NoSuchProcess:
            return
        except psutil.AccessDenied:
            log.error("access denied terminating pid=%d", pid)

    async def _wait_gone(self, pid: int, timeout_s: float) -> bool:
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            if not psutil.pid_exists(pid):
                return True
            await asyncio.sleep(0.1)
        return not psutil.pid_exists(pid)
