"""Run the agent as a Windows Service, so it starts at boot and comes back if killed.

As Administrator::

    labsentinel-service install     # install, configure recovery, start at boot
    labsentinel-service start
    labsentinel-service stop
    labsentinel-service remove      # stop, restore everything, uninstall

What ``install`` sets up, and why:

* **Automatic start** as LocalSystem, before anyone logs in.
* **Recovery actions**: restart after 5 s, 5 s, then 30 s; the failure count
  resets after a day. A participant with admin rights who kills the process
  from Task Manager gets it back within seconds, and the next start reports
  the kill to the dashboard (see lifecycle.RunMarker).
* **Failure actions also on non-zero exit**, so the loop watchdog's deliberate
  exit (a hung agent) is restarted too.
* **Tray helper at every login** via the machine-wide Run key (autostart.py).
* **The agent token readable by SYSTEM and Administrators only.**

A stop through the service manager is a normal, clean stop: the browser policy
is restored. It is still remembered and reported on the next start
("Layanan agent dihentikan secara manual"), because during an exam only an
administrator can do it. Windows shutting down is not reported.

Only :class:`ServiceRunner` and the configuration values are platform-neutral
and unit tested; the pywin32 glue below cannot be exercised off Windows and is
covered by docs/manual-test-checklist.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("labsentinel.agent.service")

SERVICE_NAME = "LabSentinelAgent"
DISPLAY_NAME = "Lab Sentinel Agent"
DESCRIPTION = (
    "Pemantauan lab komputer yang transparan: menegakkan aturan aplikasi dan situs "
    "selama sesi, dengan indikator yang selalu terlihat oleh peserta."
)
RESTART_DELAYS_MS: tuple[int, ...] = (5_000, 5_000, 30_000)
RESET_FAILURES_AFTER_S = 86_400
STOP_TIMEOUT_S = 25.0  # the SCM waits ~30 s by default before giving up on us
# Exit codes a restart cannot fix (see app.EXIT_*): not paired, pairing refused.
NO_RESTART_EXIT_CODES = frozenset({2, 3})
ERROR_SERVICE_EXISTS = 1073


class _Agent(Protocol):
    stop_reason: str

    async def run(self) -> int: ...
    async def shutdown(self) -> None: ...


class ServiceRunner:
    """Hosts the agent on its own thread and event loop.

    The service control handler runs on another thread; :meth:`stop` is safe
    to call from there and waits for a clean shutdown.
    """

    def __init__(self, make_agent: Callable[[], _Agent]) -> None:
        self._make_agent = make_agent
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[int] | None = None
        self._agent: _Agent | None = None
        self._ready = threading.Event()
        self._done = threading.Event()
        self.exit_code: int | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._main, name="ls-agent", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=30)

    def stop(self, *, reason: str = "", timeout: float = STOP_TIMEOUT_S) -> bool:
        """Ask the agent to stop; returns True if it finished in time."""
        self._ready.wait(timeout=5)
        if self._agent is not None:
            self._agent.stop_reason = reason
        loop, task = self._loop, self._task
        if loop is not None and task is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(task.cancel)
        return self._done.wait(timeout)

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)

    def _main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            self.exit_code = loop.run_until_complete(self._run())
        except Exception:
            log.exception("agent stopped with an error")
            self.exit_code = 1
        finally:
            self._ready.set()
            with contextlib.suppress(Exception):
                loop.close()
            self._done.set()

    async def _run(self) -> int:
        try:
            self._agent = self._make_agent()
        except Exception:
            self._ready.set()
            raise
        self._task = asyncio.ensure_future(self._agent.run())
        self._ready.set()
        try:
            return await self._task
        except asyncio.CancelledError:
            return 0
        finally:
            await self._agent.shutdown()


# ----------------------------------------------------------- configuration
def failure_actions(win32service: Any) -> dict[str, Any]:
    """The service-recovery settings, as ChangeServiceConfig2 expects them."""
    return {
        "ResetPeriod": RESET_FAILURES_AFTER_S,
        "RebootMsg": None,
        "Command": None,
        "Actions": [(win32service.SC_ACTION_RESTART, delay) for delay in RESTART_DELAYS_MS],
    }


def _make_agent() -> Any:
    from labsentinel_agent.app import LabSentinelAgent
    from labsentinel_shared.config import load_agent_config
    from labsentinel_shared.logging_setup import setup_logging

    path = _config_path()
    config = load_agent_config(path)
    setup_logging(log_path=config.log_path, level=config.log_level, console=False)
    return LabSentinelAgent(config, service_mode=True, config_path=path)


def _config_path() -> Path | None:
    """agent.toml next to the installed executable, else the defaults."""
    from labsentinel_agent.app import default_config_path

    return default_config_path()


# ------------------------------------------------------------- Windows glue
if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil

    class LabSentinelService(win32serviceutil.ServiceFramework):  # type: ignore[misc]
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = DISPLAY_NAME
        _svc_description_ = DESCRIPTION

        def __init__(self, args: Any) -> None:
            super().__init__(args)
            self._stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._runner = ServiceRunner(_make_agent)

        def SvcDoRun(self) -> None:  # pywin32 API names
            servicemanager.LogInfoMsg(f"{DISPLAY_NAME} starting")
            _ensure_tray_autostart()
            self._runner.start()
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            # Wait for a stop request, or for the agent to end on its own.
            while win32event.WaitForSingleObject(self._stop_event, 1000) != 0:
                if self._runner.wait(0):
                    code = self._runner.exit_code or 0
                    if code in NO_RESTART_EXIT_CODES:
                        # Not paired / pairing refused: restarting cannot fix
                        # it. Stop cleanly and say why in the event log.
                        servicemanager.LogErrorMsg(
                            f"{DISPLAY_NAME} berhenti: PC belum dipasangkan (kode {code}). "
                            "Jalankan labsentinel-agent --pair KODE --pair-only."
                        )
                    elif code:
                        # Exit without reporting SERVICE_STOPPED, which the
                        # service manager treats as a failure and restarts.
                        servicemanager.LogErrorMsg(f"{DISPLAY_NAME} exited with {code}")
                        os._exit(code)
                    break

        def SvcStop(self) -> None:
            # Someone stopped the service by hand: clean stop, but remembered.
            self._stop(reason="service_stopped")

        def SvcShutdown(self) -> None:
            # Windows is shutting down: a normal end, nothing to report.
            self._stop(reason="")

        def _stop(self, *, reason: str) -> None:
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=30_000)
            self._runner.stop(reason=reason)
            win32event.SetEvent(self._stop_event)


def _ensure_tray_autostart() -> None:  # pragma: no cover - Windows only
    """Restore the login entry for the tray helper if someone removed it."""
    from labsentinel_agent import autostart
    from labsentinel_agent.policy.registry import WinRegistry
    from labsentinel_shared.config import load_agent_config

    try:
        config = load_agent_config(_config_path())
        autostart.ensure(WinRegistry(), autostart.tray_command(data_dir=config.data_dir))
    except Exception:
        log.warning("could not check the tray autostart entry", exc_info=True)


def install() -> None:  # pragma: no cover - Windows only
    from labsentinel_agent import autostart
    from labsentinel_agent.platform.acl import restrict_to_admins
    from labsentinel_agent.policy.registry import WinRegistry
    from labsentinel_shared.config import load_agent_config

    frozen = getattr(sys, "frozen", False)
    try:
        win32serviceutil.InstallService(
            None if frozen else win32serviceutil.GetServiceClassString(LabSentinelService),
            SERVICE_NAME,
            DISPLAY_NAME,
            startType=win32service.SERVICE_AUTO_START,
            exeName=sys.executable if frozen else None,
            description=DESCRIPTION,
        )
    except Exception as exc:
        if getattr(exc, "winerror", None) != ERROR_SERVICE_EXISTS:
            raise
        # Upgrade / re-run: keep the service, refresh its settings below.
        log.info("service already installed; updating its configuration")
    scm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
    try:
        svc = win32service.OpenService(scm, SERVICE_NAME, win32service.SERVICE_ALL_ACCESS)
        try:
            win32service.ChangeServiceConfig2(
                svc, win32service.SERVICE_CONFIG_FAILURE_ACTIONS, failure_actions(win32service)
            )
            win32service.ChangeServiceConfig2(
                svc, win32service.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG, True
            )
        finally:
            win32service.CloseServiceHandle(svc)
    finally:
        win32service.CloseServiceHandle(scm)

    config = load_agent_config(_config_path())
    autostart.enable(WinRegistry(), autostart.tray_command(data_dir=config.data_dir))
    restrict_to_admins(config.token_file)  # also done whenever the token is written
    print(f"{DISPLAY_NAME} terpasang dan akan berjalan otomatis saat PC menyala.")


def remove() -> None:  # pragma: no cover - Windows only
    from labsentinel_agent import autostart
    from labsentinel_agent.policy.registry import WinRegistry
    from labsentinel_shared.config import load_agent_config

    with contextlib.suppress(Exception):
        win32serviceutil.StopService(SERVICE_NAME)
        win32serviceutil.WaitForServiceStatus(SERVICE_NAME, win32service.SERVICE_STOPPED, 30)
    win32serviceutil.RemoveService(SERVICE_NAME)
    with contextlib.suppress(OSError):
        autostart.disable(WinRegistry())
    # An uninstall is not a tamper event for a future reinstall to report.
    config = load_agent_config(_config_path())
    with contextlib.suppress(OSError):
        (config.data_dir / "running.json").unlink()
    print(f"{DISPLAY_NAME} dihapus. Kebijakan browser sudah dipulihkan.")


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - Windows only
    args = sys.argv[1:] if argv is None else argv
    if sys.platform != "win32":
        print("Layanan Windows hanya tersedia di Windows. Gunakan: labsentinel-agent")
        return 1
    if not args and getattr(sys, "frozen", False):
        # Started by the service manager.
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(LabSentinelService)
        servicemanager.StartServiceCtrlDispatcher()
        return 0
    command = args[0] if args else ""
    if command == "install":
        install()
    elif command == "remove":
        remove()
    elif command in {"start", "stop", "restart", "debug"}:
        win32serviceutil.HandleCommandLine(LabSentinelService, argv=[sys.argv[0], *args])
    else:
        print("Pemakaian: labsentinel-service install | start | stop | restart | remove")
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
