"""Drive the agent's pairing window the way a person would (Windows CI only).

    python packaging/ci/gui_pairing.py CODE OUT_DIR

Waits for the "Lab Sentinel Agent" window the installer opens, types a wrong
code, then the right one, pressing Enter like a keyboard user, and saves a
screenshot of the window at each step. Uses real keyboard input (keybd_event)
and a real screen grab, so it exercises the same path as an admin at a lab PC.
"""

from __future__ import annotations

import ctypes
import sqlite3
import sys
import time
from ctypes import wintypes
from pathlib import Path

from PIL import ImageGrab

user32 = ctypes.windll.user32  # type: ignore[attr-defined]
DB = Path(r"C:\ProgramData\LabSentinel\dashboard\labsentinel.sqlite")
DASHBOARD_LOG = Path(r"C:\ProgramData\LabSentinel\dashboard\logs\dashboard.log")
TITLE_PREFIX = "Lab Sentinel Agent"  # the pairing window
INSTALLER_TITLE = "Setup - Lab Sentinel Agent"  # Inno Setup's wizard


def find_window(timeout: float, prefix: str = TITLE_PREFIX) -> int:
    found: list[int] = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd: int, _: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value.startswith(prefix):
            found.append(hwnd)
        return True

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found.clear()
        user32.EnumWindows(proto(cb), 0)
        if found:
            return found[0]
        time.sleep(1)
    raise SystemExit(f"FAIL: no window titled {prefix!r} appeared")


def shot(hwnd: int, out: Path, name: str) -> None:
    time.sleep(0.8)
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    pad = 8
    img = ImageGrab.grab(bbox=(rect.left - pad, rect.top - pad, rect.right + pad,
                               rect.bottom + pad), all_screens=True)
    img.save(out / f"{name}.png")
    print("saved", name, img.size, flush=True)


def focus(hwnd: int) -> None:
    user32.ShowWindow(hwnd, 5)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.4)


def press(vk: int) -> None:
    user32.keybd_event(vk, 0, 0, 0)
    time.sleep(0.03)
    user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP
    time.sleep(0.05)


def type_digits(text: str) -> None:
    """Type digits and dots (enough for a code or an IPv4 address)."""
    for ch in text:
        press(0xBE if ch == "." else 0x30 + int(ch))  # 0xBE = VK_OEM_PERIOD


def alt(vk: int) -> None:
    user32.keybd_event(0x12, 0, 0, 0)  # VK_MENU down
    press(vk)
    user32.keybd_event(0x12, 0, 2, 0)
    time.sleep(0.2)


def wait_for_rejection(before: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if rejections() > before:
            return True
        time.sleep(0.5)
    return False


def paired() -> bool:
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        try:
            return bool(con.execute("SELECT 1 FROM agent_tokens").fetchone())
        finally:
            con.close()
    except sqlite3.Error:
        return False


def rejections() -> int:
    """How many agents the dashboard has turned away (e.g. for a wrong code)."""
    try:
        text = DASHBOARD_LOG.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return text.count("rejected agent from")


def main() -> None:
    code, out = sys.argv[1], Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    wrong = "100000" if code != "100000" else "100001"

    # Inno Setup always shows "Ready to Install" when it is the only page:
    # the one click an admin makes before the pairing window opens.
    setup = find_window(timeout=120, prefix=INSTALLER_TITLE)
    focus(setup)
    shot(setup, out, "0-installer-siap-dipasang")
    press(0x0D)  # Enter = Install

    hwnd = find_window(timeout=180)
    focus(hwnd)
    shot(hwnd, out, "1-jendela-pemasangan")

    type_digits(wrong)
    shot(hwnd, out, "2-kode-diketik")
    before = rejections()
    press(0x0D)  # Enter = Pasangkan
    found = wait_for_rejection(before, timeout=60)
    print("agent found the dashboard by itself (UDP discovery):", found, flush=True)
    if not found:
        # Note it for the job (a later step fails on it) and carry on with the
        # address typed in, so the rest of the installation is still checked.
        (out / "DISCOVERY-FAILED.txt").write_text("no dashboard found by discovery\n")
        shot(hwnd, out, "3a-dashboard-tidak-ditemukan")
        focus(hwnd)
        alt(0x41)  # Alt+A opens "Alamat dashboard (opsional)"
        type_digits("127.0.0.1")
        shot(hwnd, out, "3b-alamat-diisi")
        before = rejections()
        press(0x0D)
        print("wrong code reached the dashboard by address:",
              wait_for_rejection(before, timeout=60), flush=True)
    time.sleep(2)  # the window shows the reason once the agent hears back
    shot(hwnd, out, "3-kode-salah")

    focus(hwnd)
    type_digits(code)  # the wrong code is selected after an error: typing replaces it
    press(0x0D)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and not paired():
        time.sleep(1)
    time.sleep(8)  # service install + start happen after pairing
    shot(hwnd, out, "4-berhasil")
    focus(hwnd)
    press(0x0D)  # Enter = Selesai
    print("pairing window driven; paired =", paired(), flush=True)


if __name__ == "__main__":
    main()
