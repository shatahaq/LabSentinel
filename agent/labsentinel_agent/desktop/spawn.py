"""Keep a desktop worker running in whichever session is at the console.

The service runs as LocalSystem in session 0. It starts the worker with a copy
of its own SYSTEM token moved into the active console session, on that
session's interactive desktop. The worker can then see the participant's
screen, but the participant (a standard user) cannot end it.

:class:`WorkerSupervisor` holds the decisions (when to start one, how often)
and is tested everywhere; :func:`spawn_in_session` is the Windows call itself
and is verified by hand (docs/manual-test-checklist.md).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("labsentinel.agent.desktop.spawn")

NO_SESSION = 0xFFFFFFFF  # WTSGetActiveConsoleSessionId: nobody at the console
CHECK_EVERY_S = 3.0
RESPAWN_AFTER_S = 15.0  # give a fresh worker time to connect before trying again


class WorkerSupervisor:
    def __init__(
        self,
        *,
        active_session: Callable[[], int],
        connected_session: Callable[[], int | None],
        spawn: Callable[[int], bool],
        clock: Callable[[], float] = time.monotonic,
        respawn_after_s: float | None = None,
        grace_s: float = 0.0,
    ) -> None:
        self._active = active_session
        self._connected = connected_session
        self._spawn = spawn
        self._clock = clock
        self._respawn_after = RESPAWN_AFTER_S if respawn_after_s is None else respawn_after_s
        # How long a session must be missing its process before we step in;
        # lets something else (e.g. the login Run key) start it first.
        self._grace = grace_s
        self._missing_since: dict[int, float] = {}
        self._last_spawn: dict[int, float] = {}
        self.spawned = 0

    def someone_logged_in(self) -> bool:
        session = self._active()
        return session not in (NO_SESSION, -1, 0)

    def tick(self) -> bool:
        """One check. Returns True if a worker was started."""
        session = self._active()
        if session in (NO_SESSION, -1, 0):
            return False  # nobody at the console: nothing to watch
        if self._connected() == session:
            self._missing_since.pop(session, None)
            return False  # the right worker is already serving
        now = self._clock()
        missing_since = self._missing_since.setdefault(session, now)
        if now - missing_since < self._grace:
            return False
        last = self._last_spawn.get(session)
        if last is not None and self._clock() - last < self._respawn_after:
            return False  # one is starting up; do not pile them on
        self._last_spawn[session] = self._clock()
        if self._spawn(session):
            self.spawned += 1
            log.info("started a desktop worker in session %d", session)
            return True
        log.warning("could not start a desktop worker in session %d", session)
        return False

    async def run(self, every_s: float = CHECK_EVERY_S) -> None:
        while True:
            try:
                await asyncio.to_thread(self.tick)
            except Exception:
                log.exception("desktop worker supervision failed")
            await asyncio.sleep(every_s)


def worker_command(config_path: Path | None) -> str:
    """Command line for the worker: the installed exe, or this Python."""
    if getattr(sys, "frozen", False):
        cmd = f'"{Path(sys.executable).with_name("labsentinel-agent.exe")}" --desktop-worker'
    else:
        cmd = f'"{sys.executable}" -m labsentinel_agent --desktop-worker'
    if config_path is not None:
        cmd += f' --config "{config_path}"'
    return cmd


def active_console_session() -> int:
    if sys.platform != "win32":
        return NO_SESSION
    import win32ts

    return int(win32ts.WTSGetActiveConsoleSessionId())


def spawn_as_user(session_id: int, command: str) -> bool:  # pragma: no cover - Windows
    """Start ``command`` as the user logged in to ``session_id`` (the tray helper).

    The indicator must run as the participant, in their session, so it shows
    in their notification area. Used when the helper has gone missing.
    """
    if sys.platform != "win32":
        return False
    import win32api
    import win32con
    import win32process
    import win32profile
    import win32ts

    try:
        token = win32ts.WTSQueryUserToken(session_id)
        env = win32profile.CreateEnvironmentBlock(token, False)
        startup = win32process.STARTUPINFO()
        startup.lpDesktop = "winsta0\\default"
        proc, thread, _pid, _tid = win32process.CreateProcessAsUser(
            token, None, command, None, None, False,
            win32con.CREATE_UNICODE_ENVIRONMENT, env, None, startup,
        )
        win32api.CloseHandle(thread)
        win32api.CloseHandle(proc)
        return True
    except Exception:
        log.exception("could not start the tray helper in session %d", session_id)
        return False


def spawn_in_session(session_id: int, command: str) -> bool:  # pragma: no cover - Windows
    """Start ``command`` as SYSTEM inside ``session_id`` on its interactive desktop."""
    if sys.platform != "win32":
        return False
    import win32api
    import win32con
    import win32process
    import win32security

    try:
        own = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(),
            win32security.TOKEN_DUPLICATE | win32security.TOKEN_QUERY
            | win32security.TOKEN_ASSIGN_PRIMARY | win32security.TOKEN_ADJUST_DEFAULT
            | win32security.TOKEN_ADJUST_SESSIONID,
        )
        token = win32security.DuplicateTokenEx(
            own, win32security.SecurityImpersonation, win32security.TOKEN_ALL_ACCESS,
            win32security.TokenPrimary, None,
        )
        win32security.SetTokenInformation(token, win32security.TokenSessionId, session_id)
        startup = win32process.STARTUPINFO()
        startup.lpDesktop = "winsta0\\default"
        startup.dwFlags = win32con.STARTF_USESHOWWINDOW
        startup.wShowWindow = win32con.SW_HIDE
        proc, thread, _pid, _tid = win32process.CreateProcessAsUser(
            token, None, command, None, None, False,
            win32process.CREATE_NO_WINDOW | win32process.CREATE_NEW_PROCESS_GROUP,
            None, None, startup,
        )
        win32api.CloseHandle(thread)
        win32api.CloseHandle(proc)
        return True
    except Exception:
        log.exception("CreateProcessAsUser into session %d failed", session_id)
        return False
