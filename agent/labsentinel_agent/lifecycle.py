"""Process lifecycle guards: one instance, honest exits, no silent hangs.

Three small pieces the anti-tamper story is built from, all platform-neutral so
they are tested everywhere:

* :class:`InstanceLock` — only one agent per PC. Two copies with the same
  identity would keep replacing each other's connection on the dashboard.
* :class:`RunMarker` — a file that exists only while the agent runs. If the
  agent starts and finds one, the previous run did not end normally: it was
  killed, crashed, or the PC lost power. That is reported to the dashboard.
* :class:`LoopWatchdog` — a thread that notices when the agent's event loop
  stops turning (a hung driver call, a deadlock) and exits the process, so the
  Windows Service manager restarts it instead of leaving a PC unmonitored while
  looking alive.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

log = logging.getLogger("labsentinel.agent.lifecycle")

EXIT_WATCHDOG = 70  # distinct, so the service log shows why it restarted


# ------------------------------------------------------------ single instance
class InstanceLock:
    """An OS-level exclusive lock on a file; released automatically on exit.

    The OS drops the lock when the process dies, however it dies, so a crash
    never leaves a stale lock that blocks the next start.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._fh: IO[Any] | None = None

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._path, "a+")  # noqa: SIM115 (held for the process lifetime)
        try:
            if sys.platform == "win32":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is None:
            return
        with contextlib.suppress(OSError):
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            self._fh.close()
        self._fh = None


# ---------------------------------------------------------- abnormal exits
@dataclass(frozen=True)
class PreviousRun:
    """What the marker says about a run that did not end normally."""

    pid: int
    started_at: float
    last_alive: float
    reason: str  # "" = unknown (killed / crashed); "service_stopped" = stopped by hand
    rebooted_since: bool  # the PC itself restarted after the agent was last alive

    def describe(self) -> str:
        """Plain-language detail for the violation record."""
        at = time.strftime("%H:%M:%S", time.localtime(self.last_alive))
        if self.reason == "service_stopped":
            return f"Layanan agent dihentikan secara manual (terakhir aktif {at})."
        if self.rebooted_since:
            return (
                f"PC mati atau restart tanpa shutdown normal (terakhir aktif {at}); "
                "misalnya tombol power ditekan atau listrik padam."
            )
        return f"Agent dihentikan paksa saat PC menyala (terakhir aktif {at})."


class RunMarker:
    """A small JSON file present only while the agent is running."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
        boot_time: Callable[[], float] | None = None,
    ) -> None:
        self._path = path
        self._clock = clock
        self._boot_time = boot_time or _boot_time
        self._started_at = 0.0

    def begin(self) -> PreviousRun | None:
        """Start a run. Returns the previous run if it ended abnormally."""
        previous = self._read()
        self._started_at = self._clock()
        self._write(last_alive=self._started_at, reason="")
        if previous is None:
            return None
        last_alive = float(previous.get("last_alive", 0.0))
        try:
            rebooted = self._boot_time() > last_alive
        except Exception:
            rebooted = False
        return PreviousRun(
            pid=int(previous.get("pid", 0)),
            started_at=float(previous.get("started_at", 0.0)),
            last_alive=last_alive,
            reason=str(previous.get("reason", "")),
            rebooted_since=rebooted,
        )

    def touch(self) -> None:
        """Record that we are still alive (called periodically)."""
        self._write(last_alive=self._clock(), reason="")

    def end(self, *, reason: str = "") -> None:
        """Finish a run.

        ``reason=""`` is a normal end (the marker is removed). Any other reason
        is kept on disk so the next start reports how the agent stopped, e.g.
        ``"service_stopped"`` when someone stopped the service by hand.
        """
        if reason:
            self._write(last_alive=self._clock(), reason=reason)
            return
        with contextlib.suppress(OSError):
            self._path.unlink()

    def _read(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            # Unreadable marker: the previous run still did not end cleanly.
            return {"last_alive": 0.0}
        return data if isinstance(data, dict) else {"last_alive": 0.0}

    def _write(self, *, last_alive: float, reason: str) -> None:
        body = json.dumps({
            "pid": os.getpid(), "started_at": self._started_at,
            "last_alive": last_alive, "reason": reason,
        })
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".run-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.replace(tmp, self._path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            log.warning("could not update the run marker", exc_info=True)


def _boot_time() -> float:
    import psutil

    return float(psutil.boot_time())


# ---------------------------------------------------------------- watchdog
class LoopWatchdog:
    """Exit the process if the asyncio loop stops responding.

    The loop side calls :meth:`beat` regularly (see :meth:`run_beats`); a
    daemon thread checks the age of the last beat. Exiting is deliberate: the
    service manager restarts a dead process, but nothing restarts a hung one.
    """

    def __init__(
        self,
        *,
        stall_s: float = 90.0,
        check_every_s: float = 5.0,
        on_stall: Callable[[float], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._stall_s = stall_s
        self._check_every = check_every_s
        self._clock = clock
        self._on_stall = on_stall or _exit_for_restart
        self._last = clock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.tripped = False

    def beat(self) -> None:
        self._last = self._clock()

    async def run_beats(self, interval_s: float = 1.0) -> None:
        while True:
            self.beat()
            await asyncio.sleep(interval_s)

    def start(self) -> None:
        self.beat()
        self._thread = threading.Thread(target=self._watch, name="ls-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def check(self) -> bool:
        """One check; returns True if the loop is stalled (and acts on it)."""
        age = self._clock() - self._last
        if age <= self._stall_s or self.tripped:
            return False
        self.tripped = True
        log.critical("agent event loop unresponsive for %.0fs; exiting for restart", age)
        self._on_stall(age)
        return True

    def _watch(self) -> None:
        while not self._stop.wait(self._check_every):
            self.check()


def _exit_for_restart(_age: float) -> None:
    logging.shutdown()
    os._exit(EXIT_WATCHDOG)
