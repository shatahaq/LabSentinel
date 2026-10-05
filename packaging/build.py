"""Build the agent and dashboard folders, then (on Windows) the installers.

    python packaging/build.py              # both PyInstaller builds
    python packaging/build.py --installer  # ... and Inno Setup installers

Outputs:
    build/dist/LabSentinelAgent/        build/dist/LabSentinelDashboard/
    build/installer/LabSentinelAgent-Setup-<versi>.exe
    build/installer/LabSentinelDashboard-Setup-<versi>.exe

Inno Setup 6 (iscc.exe) must be installed for --installer; it is looked up on
PATH and in its default install folder.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "build" / "dist"
WORK = ROOT / "build" / "work"
OUT = ROOT / "build" / "installer"


def version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def pyinstaller(spec: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(ROOT / "packaging" / spec),
         "--noconfirm", "--clean", "--distpath", str(DIST), "--workpath", str(WORK)],
        check=True, cwd=ROOT,
    )


def find_iscc() -> str | None:
    found = shutil.which("iscc")
    if found:
        return found
    for base in (
        Path("C:/Program Files (x86)/Inno Setup 6"),
        Path("C:/Program Files/Inno Setup 6"),
    ):
        if (base / "ISCC.exe").exists():
            return str(base / "ISCC.exe")
    return None


def installers() -> None:
    iscc = find_iscc()
    if iscc is None:
        raise SystemExit("Inno Setup 6 (iscc.exe) tidak ditemukan. Pasang dari jrsoftware.org.")
    OUT.mkdir(parents=True, exist_ok=True)
    for script in ("agent.iss", "dashboard.iss"):
        subprocess.run(
            [iscc, f"/DAppVersion={version()}", f"/DDistDir={DIST}", f"/O{OUT}",
             str(ROOT / "packaging" / "installer" / script)],
            check=True, cwd=ROOT,
        )


def main() -> int:
    parser = argparse.ArgumentParser(prog="build")
    parser.add_argument("--installer", action="store_true", help="Juga buat installer (Windows).")
    parser.add_argument("--only", choices=["agent", "dashboard"], default=None)
    args = parser.parse_args()

    if args.only in (None, "agent"):
        pyinstaller("agent.spec")
    if args.only in (None, "dashboard"):
        pyinstaller("dashboard.spec")
    if args.installer:
        if sys.platform != "win32":
            raise SystemExit("Installer hanya bisa dibuat di Windows.")
        installers()
    print(f"Selesai. Hasil di {DIST}" + (f" dan {OUT}" if args.installer else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
