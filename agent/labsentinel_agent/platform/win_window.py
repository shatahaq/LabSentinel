"""Foreground-window title + agent self-metrics (CPU/RAM).

Reading the active window title is the supplementary signal for site-title
violation logging (M4). Self-metrics let the agent report its own footprint so
the NFR target (CPU < 2%, RAM < 100 MB) can be observed from the dashboard.
"""

from __future__ import annotations

import os
import sys

import psutil

_IS_WINDOWS = sys.platform == "win32"
_proc = psutil.Process(os.getpid())
# Prime cpu_percent so the first real call returns a meaningful delta.
_proc.cpu_percent(None)

if _IS_WINDOWS:
    import win32gui


def active_window_title() -> str:
    if not _IS_WINDOWS:
        return ""
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return ""
        return win32gui.GetWindowText(hwnd) or ""
    except Exception:
        return ""


def agent_metrics() -> tuple[float, float, str]:
    """Return (cpu_percent, mem_mb, active_window_title) for this agent process."""
    try:
        cpu = _proc.cpu_percent(None)
        mem_mb = _proc.memory_info().rss / (1024 * 1024)
    except psutil.Error:
        cpu, mem_mb = 0.0, 0.0
    # Normalize cpu_percent (can exceed 100 across cores) into the 0..100 field.
    cpu = max(0.0, min(100.0, cpu))
    return cpu, mem_mb, active_window_title()


def lock_workstation() -> bool:
    """Lock the screen (participant must re-enter credentials). Windows only."""
    if not _IS_WINDOWS:
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.LockWorkStation())
    except Exception:
        return False
