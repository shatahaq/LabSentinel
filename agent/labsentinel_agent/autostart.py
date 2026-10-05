"""Start the tray helper whenever anyone logs in to a lab PC.

The service runs in session 0 and cannot draw on the desktop (ADR-0004), so the
participant-visible indicator is a separate helper that must start in every
user session. The machine-wide ``Run`` key does exactly that, for every account,
and only an administrator can change it.

The service re-checks the entry on every start and restores it if it was
removed, so deleting it once does not quietly switch the indicator off for
good. (Removing the indicator does not stop monitoring either; the dashboard
shows "indikator disembunyikan" for that PC.)
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from labsentinel_agent.policy.registry import Hive, RegistryPort, ValueType

log = logging.getLogger("labsentinel.agent.autostart")

RUN_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "LabSentinelTray"
TRAY_EXE = "labsentinel-tray.exe"


def tray_command(*, data_dir: Path | None = None, frozen: bool | None = None) -> str:
    """The command line Windows runs at login.

    Installed (PyInstaller) builds start the windowless tray executable next to
    the agent; a source checkout starts ``pythonw -m labsentinel_agent.tray``.
    """
    is_frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if is_frozen:
        cmd = f'"{Path(sys.executable).with_name(TRAY_EXE)}"'
    else:
        exe = Path(sys.executable)
        pythonw = exe.with_name("pythonw.exe")
        runner = pythonw if pythonw.exists() else exe
        cmd = f'"{runner}" -m labsentinel_agent.tray'
    if data_dir is not None:
        cmd += f' --data-dir "{data_dir}"'
    return cmd


def current(registry: RegistryPort) -> str | None:
    values = registry.read_values(Hive.HKLM, RUN_KEY)
    entry = values.get(VALUE_NAME)
    return str(entry[0]) if entry else None


def enable(registry: RegistryPort, command: str) -> None:
    registry.create_key(Hive.HKLM, RUN_KEY)
    registry.write_value(Hive.HKLM, RUN_KEY, VALUE_NAME, command, ValueType.SZ)
    log.info("tray helper will start at every login: %s", command)


def disable(registry: RegistryPort) -> None:
    """Remove only our entry; other programs' Run entries are untouched."""
    registry.delete_value(Hive.HKLM, RUN_KEY, VALUE_NAME)


def ensure(registry: RegistryPort, command: str) -> bool:
    """Put the entry back if it is missing or was changed. True if repaired."""
    try:
        if current(registry) == command:
            return False
        enable(registry, command)
    except OSError:
        log.warning("could not verify the tray autostart entry", exc_info=True)
        return False
    log.warning("tray autostart entry was missing or altered; restored it")
    return True
