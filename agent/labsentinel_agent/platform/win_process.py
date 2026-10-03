"""Windows process enumeration, window mapping, and process control.

Builds ``ProcessInfo`` snapshots (pid, name, path, owner, session, visible
window + title) from psutil + pywin32, and implements graceful close via
``WM_CLOSE`` with a hard-kill fallback. The visible-window map is what lets the
safety gate restrict enforcement to user-facing apps.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import psutil

from labsentinel_agent.enforce.identity import SYSTEM_ACCOUNTS, ProcessInfo

log = logging.getLogger("labsentinel.agent.platform")

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import win32con
    import win32gui
    import win32process
    import win32ts


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


def build_window_index() -> dict[int, str]:
    """Map pid -> a representative visible window title (one per pid)."""
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
    )


class WinProcessController:
    """Graceful close via WM_CLOSE, hard kill via psutil as a fallback."""

    def is_running(self, pid: int) -> bool:
        return bool(psutil.pid_exists(pid))

    async def graceful_close(self, pid: int, timeout_s: float) -> bool:
        if _IS_WINDOWS:
            self._post_wm_close(pid)
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

    def _post_wm_close(self, pid: int) -> None:
        def _cb(hwnd: int, _: object) -> bool:
            try:
                _, wpid = win32process.GetWindowThreadProcessId(hwnd)
                if wpid == pid and win32gui.IsWindowVisible(hwnd):
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(_cb, None)
        except Exception:
            log.debug("EnumWindows during close failed", exc_info=True)

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
