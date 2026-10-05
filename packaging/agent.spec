# PyInstaller spec: the agent installation folder.
#
#   pyinstaller packaging/agent.spec --distpath build/dist --workpath build/work
#
# Produces one folder, LabSentinelAgent/, holding three programs that share a
# single copy of Python and the libraries:
#
#   labsentinel-service.exe  Windows Service host (session 0, LocalSystem)
#   labsentinel-agent.exe    pairing (--pair KODE --pair-only) and diagnostics
#   labsentinel-tray.exe     the participant's tray indicator (no console window)
#
# Build on Windows for a real installation. A build on Linux only proves the
# spec and the pure-Python parts; pywin32/WMI/pystray are Windows-only.
# ruff: noqa  (PyInstaller injects Analysis, PYZ, EXE, COLLECT, SPECPATH)

import sys
from pathlib import Path

ROOT = Path(SPECPATH).parent
PATHS = [str(ROOT / "shared"), str(ROOT / "agent")]

WINDOWS_ONLY = [
    "win32timezone",  # needed by pywin32 services, not detected automatically
    "win32serviceutil", "win32service", "win32event", "servicemanager",
    "win32ts", "win32gui", "win32process", "win32con", "wmi", "pythoncom",
    "pystray._win32",
]
HIDDEN = [
    "labsentinel_agent.platform.win_process",
    "labsentinel_agent.platform.win_window",
    "labsentinel_agent.platform.win_detector",
    "labsentinel_agent.policy.registry",
] + (WINDOWS_ONLY if sys.platform == "win32" else [])
EXCLUDES = ["tkinter", "eel", "gevent", "reportlab", "openpyxl", "labsentinel_dashboard"]


def analysis(script: str) -> "Analysis":
    return Analysis(
        [str(ROOT / "packaging" / "entry" / script)],
        pathex=PATHS,
        hiddenimports=HIDDEN,
        excludes=EXCLUDES,
        noarchive=False,
    )


a_service = analysis("service.py")
a_agent = analysis("agent.py")
a_tray = analysis("tray.py")


def exe(a: "Analysis", name: str, *, console: bool) -> "EXE":
    return EXE(
        PYZ(a.pure),
        a.scripts,
        [],
        exclude_binaries=True,
        name=name,
        console=console,
        uac_admin=False,  # the installer runs elevated; nothing here should prompt
        version=None,
    )


COLLECT(
    exe(a_service, "labsentinel-service", console=True),
    exe(a_agent, "labsentinel-agent", console=True),
    exe(a_tray, "labsentinel-tray", console=False),
    a_service.binaries, a_service.datas,
    a_agent.binaries, a_agent.datas,
    a_tray.binaries, a_tray.datas,
    name="LabSentinelAgent",
)
