# PyInstaller spec: the supervisor's dashboard.
#
#   pyinstaller packaging/dashboard.spec --distpath build/dist --workpath build/work
#
# Produces LabSentinelDashboard/labsentinel-dashboard.exe with the admin UI
# (dashboard/web) bundled as "web" and Eel's own eel.js.
# ruff: noqa  (PyInstaller injects Analysis, PYZ, EXE, COLLECT, SPECPATH)

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).parent

a = Analysis(
    [str(ROOT / "packaging" / "entry" / "dashboard.py")],
    pathex=[str(ROOT / "shared"), str(ROOT / "dashboard")],
    datas=[(str(ROOT / "dashboard" / "web"), "web"), *collect_data_files("eel")],
    hiddenimports=[
        "bottle_websocket",
        "geventwebsocket",  # Eel's transport (gevent-websocket)
        "labsentinel_dashboard.reports.pdf",
        "labsentinel_dashboard.reports.xlsx",
    ],
    excludes=["tkinter", "labsentinel_agent", "psutil", "mss"],
)

exe = EXE(
    PYZ(a.pure),
    a.scripts,
    [],
    exclude_binaries=True,
    name="labsentinel-dashboard",
    console=True,  # shows the address and pairing code; closing it stops the dashboard
)

COLLECT(exe, a.binaries, a.datas, name="LabSentinelDashboard")
