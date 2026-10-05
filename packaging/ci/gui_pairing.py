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
TITLE_PREFIX = "Lab Sentinel Agent"


def find_window(timeout: float) -> int:
    found: list[int] = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd: int, _: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if buf.value.startswith(TITLE_PREFIX):
            found.append(hwnd)
        return True

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found.clear()
        user32.EnumWindows(proto(cb), 0)
        if found:
            return found[0]
        time.sleep(1)
    raise SystemExit("FAIL: the pairing window did not appear")


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
    for ch in text:
        press(0x30 + int(ch))


def paired() -> bool:
    try:
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=5)
        try:
            return bool(con.execute("SELECT 1 FROM agent_tokens").fetchone())
        finally:
            con.close()
    except sqlite3.Error:
        return False


def main() -> None:
    code, out = sys.argv[1], Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    wrong = "100000" if code != "100000" else "100001"

    hwnd = find_window(timeout=180)
    focus(hwnd)
    shot(hwnd, out, "1-jendela-pemasangan")

    type_digits(wrong)
    shot(hwnd, out, "2-kode-diketik")
    press(0x0D)  # Enter = Pasangkan
    time.sleep(6)
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
