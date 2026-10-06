"""Freeze the participant's keyboard and mouse, leaving the screen untouched.

A supervisor uses this from the dashboard on a PC caught cheating: the input
goes dead while the screen keeps showing exactly what the participant had open,
so the supervisor can walk over and see it. Nothing is drawn over the screen.

It runs inside the desktop worker, which the service starts as SYSTEM in the
participant's own session, so ``BlockInput`` there blocks the participant's
input but not the worker's own thread.

Fail-safe, by design: Windows releases ``BlockInput`` automatically when the
calling process exits, and a participant can always break it with
Ctrl+Alt+Del. So a crashed agent, a killed worker or the secure-attention key
all return control to the participant -- freezing can never strand them.

Everything here is Windows-only and exercised by hand (see
docs/manual-test-checklist.md); off Windows the calls are no-ops.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading

log = logging.getLogger("labsentinel.agent.freeze")

_IS_WINDOWS = sys.platform == "win32"

# BlockInput can be quietly released (e.g. the secure-attention sequence), so
# the holder re-asserts it on this cadence while a freeze is in effect.
_REASSERT_EVERY_S = 0.2


class _Freezer:
    """Holds the one active freeze (a worker never needs two at once)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._frozen = False

    @property
    def frozen(self) -> bool:
        return self._frozen

    def set(self, on: bool) -> bool:
        if not _IS_WINDOWS:
            return False
        with self._lock:
            if on:
                if self._frozen:
                    return True
                self._stop.clear()
                self._thread = threading.Thread(
                    target=self._hold, name="ls-freeze", daemon=True
                )
                self._thread.start()
                self._frozen = True
                return True
            # Release.
            if not self._frozen:
                return True
            self._stop.set()
            thread = self._thread
            if thread is not None:
                thread.join(timeout=3.0)
            self._thread = None
            self._frozen = False
            return True

    def _hold(self) -> None:  # pragma: no cover - Windows input API
        """Own the block on a dedicated thread.

        ``BlockInput`` blocks every thread except the one that called it, so
        this thread keeps re-asserting it while the participant's input is held.
        The screen is left exactly as the participant had it -- nothing is drawn.
        """
        # windll exists only on Windows: the ignore is needed elsewhere only.
        user32 = ctypes.windll.user32  # type: ignore[attr-defined, unused-ignore]
        try:
            while not self._stop.is_set():
                # Re-assert: a released block (e.g. after Ctrl+Alt+Del) is retaken.
                user32.BlockInput(True)
                self._stop.wait(_REASSERT_EVERY_S)
        except Exception:
            log.warning("freeze loop failed", exc_info=True)
        finally:
            try:
                user32.BlockInput(False)
            except Exception:
                log.debug("BlockInput release failed", exc_info=True)


_freezer = _Freezer()


def set_input_frozen(on: bool) -> bool:
    """Freeze (``on``) or release the keyboard and mouse. Windows only."""
    return _freezer.set(on)


def input_is_frozen() -> bool:
    return _freezer.frozen
