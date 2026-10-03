"""Warning presenters (WarningPresenter port).

``LoggingWarningPresenter`` always works and is used in dev/tests. On a real
lab PC the on-screen, non-focus-stealing warning is delivered by the system-tray
notifier introduced in M3 (which implements this same port) — so enforcement
code never depends on a particular UI mechanism.
"""

from __future__ import annotations

import logging

log = logging.getLogger("labsentinel.agent.warning")


class LoggingWarningPresenter:
    """Records warnings to the log. Safe no-op UI for headless/dev use."""

    def __init__(self) -> None:
        self.last_message: str | None = None

    def warn_closing(self, process_name: str, seconds: int, reason: str) -> None:
        msg = (
            f"Aplikasi '{process_name}' tidak diizinkan dan akan ditutup dalam "
            f"{seconds} detik. ({reason})"
        )
        self.last_message = msg
        log.info("warn participant: %s", msg)

    def notify(self, title: str, message: str) -> None:
        log.info("notify participant: %s — %s", title, message)
