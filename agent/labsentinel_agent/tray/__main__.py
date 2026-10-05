"""Tray helper entrypoint — runs in the participant's desktop session.

    python -m labsentinel_agent.tray [--data-dir DIR]

pystray must own the main thread (it pumps the Win32 message loop), so the IPC
client runs in a background asyncio thread and drives the icon from there.

This process is display-only. It cannot pause monitoring or change enforcement;
killing it only removes the indicator, which the service reports to the
dashboard.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
from pathlib import Path

from labsentinel_agent.tray.helper import TrayHelper
from labsentinel_agent.tray.icon import NullIndicator, PystrayIndicator, TrayIndicator
from labsentinel_shared.config import AgentConfig
from labsentinel_shared.logging_setup import setup_logging

log = logging.getLogger("labsentinel.agent.tray.main")


def _paths(data_dir: Path) -> tuple[Path, Path]:
    return data_dir / "tray_token", data_dir / "tray_endpoint"


def tray_log_path(data_dir: Path) -> Path:
    """Where this helper logs.

    The helper runs as whoever is logged in, so on Windows it logs to that
    user's own profile: a shared file in the agent's data folder would be
    owned by the first participant and unwritable for the next one.
    """
    local = os.environ.get("LOCALAPPDATA")
    if sys.platform == "win32" and local:
        return Path(local) / "LabSentinel" / "tray.log"
    return data_dir / "logs" / "tray.log"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="labsentinel-tray")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=AgentConfig().data_dir,
        help="Agent data directory holding tray_token and tray_endpoint.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without a real tray icon (diagnostics).",
    )
    args = parser.parse_args(argv)

    try:
        setup_logging(log_path=tray_log_path(args.data_dir), level="INFO")
    except OSError:
        # Never let an unwritable log file take the indicator away.
        logging.basicConfig(level=logging.INFO)

    indicator: TrayIndicator
    if args.headless or sys.platform != "win32":
        indicator = NullIndicator()
    else:
        indicator = PystrayIndicator()

    token_file, endpoint_file = _paths(args.data_dir)
    helper = TrayHelper(
        indicator=indicator, token_file=token_file, endpoint_file=endpoint_file
    )

    loop = asyncio.new_event_loop()

    def _run_ipc() -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(helper.run())
        except Exception:
            log.exception("tray helper loop stopped")

    thread = threading.Thread(target=_run_ipc, name="tray-ipc", daemon=True)
    thread.start()

    try:
        # Blocks pumping the UI loop (returns immediately for NullIndicator).
        indicator.run()
        if isinstance(indicator, NullIndicator):
            thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        loop.call_soon_threadsafe(helper.stop)
        thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
