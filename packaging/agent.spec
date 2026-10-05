# PyInstaller spec: the agent installation folder.
#
#   pyinstaller packaging/agent.spec --distpath build/dist --workpath build/work
#
# Produces one folder, LabSentinelAgent/, holding four programs that share a
# single copy of Python and the libraries:
#
#   labsentinel-setup.exe    the pairing window an admin uses (no console, asks UAC)
#   labsentinel-service.exe  Windows Service host (session 0, LocalSystem)
#   labsentinel-agent.exe    command line: --pair KODE --pair-only, diagnostics,
#                            and the desktop worker the service starts
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


def analysis(script: str, *, gui: bool = False) -> "Analysis":
    excludes = [m for m in EXCLUDES if not (gui and m == "tkinter")]
    return Analysis(
        [str(ROOT / "packaging" / "entry" / script)],
        pathex=PATHS,
        hiddenimports=HIDDEN,
        excludes=excludes,
        noarchive=False,
    )


a_setup = analysis("setup.py", gui=True)  # only this one needs tkinter
a_service = analysis("service.py")
a_agent = analysis("agent.py")
a_tray = analysis("tray.py")


def exe(a: "Analysis", name: str, *, console: bool, admin: bool = False) -> "EXE":
    return EXE(
        PYZ(a.pure),
        a.scripts,
        [],
        exclude_binaries=True,
        name=name,
        console=console,
        # Only the setup window asks for administrator rights (it writes the
        # token and installs the service). Started by the elevated installer
        # it does not prompt a second time.
        uac_admin=admin,
        version=None,
    )


COLLECT(
    exe(a_setup, "labsentinel-setup", console=False, admin=True),
    exe(a_service, "labsentinel-service", console=True),
    exe(a_agent, "labsentinel-agent", console=True),
    exe(a_tray, "labsentinel-tray", console=False),
    a_setup.binaries, a_setup.datas,
    a_service.binaries, a_service.datas,
    a_agent.binaries, a_agent.datas,
    a_tray.binaries, a_tray.datas,
    name="LabSentinelAgent",
)
