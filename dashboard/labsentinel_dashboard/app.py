"""Dashboard entry point: starts the agent server and the admin UI.

Two runtimes live in one process (see ADR-0001):

* an **asyncio loop in a worker thread** running the agent-facing WebSocket
  server, the UDP discovery beacon, the heartbeat watchdog and the session
  scheduler;
* **Eel/gevent on the main thread** serving the admin UI on localhost only.

They are joined by :class:`UiBridge`, which is the only place the two threading
models meet.

    python -m labsentinel_dashboard [--config PATH] [--no-browser]
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import logging
import socket
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from labsentinel_dashboard.domain.events import EventBus
from labsentinel_dashboard.domain.pairing import PairingService
from labsentinel_dashboard.domain.rules_service import RulesService
from labsentinel_dashboard.domain.service import DashboardService
from labsentinel_dashboard.domain.session_presets import PROFILE_LABELS  # noqa: F401
from labsentinel_dashboard.domain.sessions import SessionService
from labsentinel_dashboard.domain.site_presets import rules_for_profile
from labsentinel_dashboard.storage.db import Database
from labsentinel_dashboard.transport.server import WebSocketServer
from labsentinel_dashboard.ui.bridge import UiBridge
from labsentinel_shared.config import DashboardConfig, load_dashboard_config
from labsentinel_shared.discovery import Beacon, DashboardBeaconServer
from labsentinel_shared.logging_setup import setup_logging
from labsentinel_shared.rules import RuleSet

log = logging.getLogger("labsentinel.dashboard.app")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Bridge methods exposed to the UI. Listed explicitly so nothing is published
# to the browser by accident.
EXPOSED = (
    "poll_events", "get_overview", "get_agents", "get_frame", "set_viewing",
    "pause", "resume", "lock_screens", "broadcast",
    "get_rules", "set_mode", "set_dry_run", "set_whitelist",
    "set_site_categories", "set_custom_sites", "set_close_grace",
    "start_session", "end_session", "schedule_session", "cancel_session", "list_sessions",
    "get_violations", "get_screenshot", "get_audit",
    "get_pairing", "rotate_pairing_code", "set_pairing_open",
)


def _named_proxy(bridge: UiBridge, name: str) -> Callable[..., Any]:
    """Wrap a bound method in a function Eel can register under ``name``."""
    method = getattr(bridge, name)

    @functools.wraps(method)
    def proxy(*args: Any, **kwargs: Any) -> Any:
        return method(*args, **kwargs)

    proxy.__name__ = name
    return proxy


def local_ip() -> str:
    """Best-guess LAN address to advertise in the discovery beacon."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))  # no packet is sent; picks the route
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class DashboardApp:
    def __init__(self, config: DashboardConfig) -> None:
        self.config = config
        self.db = Database(config.db_path)
        self.bus = EventBus()
        default_rules = RuleSet(
            revision=1,
            mode=config.default_mode,
            dry_run=config.default_dry_run,
            site_blocklist=rules_for_profile(str(config.default_mode)),
        )
        self.rules = RulesService(self.db, default=default_rules)
        self.service = DashboardService(
            db=self.db,
            bus=self.bus,
            rules_provider=self.rules.current,
            screenshot_dir=config.screenshot_dir,
            heartbeat_timeout_s=config.heartbeat_timeout_s,
        )
        self.sessions = SessionService(
            db=self.db, service=self.service, rules=self.rules, bus=self.bus
        )
        self.pairing = PairingService(self.db)
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._server: WebSocketServer | None = None
        self._beacon: DashboardBeaconServer | None = None

    # ---------------------------------------------------------- background
    def start_background(self) -> None:
        self._thread = threading.Thread(target=self._run_loop, name="ls-agents", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=15):
            raise RuntimeError("agent server did not start in time")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._serve())
        except (asyncio.CancelledError, RuntimeError):
            # Expected while shutting down: the loop is stopped under us.
            log.debug("agent server loop ended", exc_info=True)
        except Exception:
            log.exception("agent server loop crashed")
        finally:
            with contextlib.suppress(Exception):
                self.loop.close()

    async def _serve(self) -> None:
        self._server = WebSocketServer(
            host=self.config.ws_host,
            port=self.config.ws_port,
            authenticator=self.pairing,
            ruleset_provider=self.rules.ruleset_for,
            events=self.service,
            heartbeat_interval_s=5.0,
        )
        await self._server.start()
        self.service.attach(self._server)

        advertise = self.config.advertise_host or local_ip()
        self._beacon = DashboardBeaconServer(
            Beacon(
                ws_host=advertise,
                ws_port=self._server.bound_port,
                dashboard_name=self.config.dashboard_name,
            ),
            port=self.config.discovery_port,
            interval_s=self.config.beacon_interval_s,
        )
        await self._beacon.start()

        log.info(
            "agents can reach this dashboard at ws://%s:%d (announced on UDP %d)",
            advertise, self._server.bound_port, self.config.discovery_port,
        )
        self._ready.set()

        await asyncio.gather(
            self.service.run_watchdog(),
            self.sessions.run(),
            self._retention_loop(),
        )

    async def _retention_loop(self) -> None:
        """Apply the data-retention policy once an hour."""
        while True:
            with contextlib.suppress(Exception):
                removed = self.service.purge_expired(self.config.retention_days)
                if removed:
                    log.info("retention removed %d old screenshots", removed)
            await asyncio.sleep(3600)

    # ----------------------------------------------------------------- ui
    def run_ui(self, *, open_browser: bool = True, keep_running: bool = False) -> None:
        """Serve the admin UI.

        ``keep_running`` stops Eel from exiting when the last browser window
        closes, so an accidentally-closed window during an exam does not stop
        the dashboard recording violations. The admin reopens the page at the
        printed URL.
        """
        import eel

        bridge = UiBridge(
            service=self.service, sessions=self.sessions, rules=self.rules,
            db=self.db, bus=self.bus, loop=self.loop, pairing=self.pairing,
        )
        eel.init(str(WEB_DIR))
        for name in EXPOSED:
            # Eel registers by __name__, which a bound method will not let us
            # set, so each method is wrapped in a plain named function.
            eel.expose(_named_proxy(bridge, name))

        self._print_banner()
        try:
            eel.start(
                "index.html",
                host=self.config.eel_host,
                port=self.config.eel_port,
                size=(1440, 900),
                mode="chrome" if open_browser else None,
                # A very long delay effectively disables the auto-exit.
                shutdown_delay=86400.0 if keep_running else 2.0,
            )
        except (SystemExit, KeyboardInterrupt):
            pass
        except OSError as exc:
            log.error("could not open the admin window: %s", exc)
            print(
                f"\nBuka dashboard secara manual di: "
                f"http://{self.config.eel_host}:{self.config.eel_port}/index.html\n"
            )
            raise

    def _print_banner(self) -> None:
        port = self._server.bound_port if self._server else self.config.ws_port
        print("=" * 66)
        print("  Lab Sentinel — Dashboard Pengawas")
        print("=" * 66)
        print(f"  Antarmuka admin : http://{self.config.eel_host}:{self.config.eel_port}")
        print(f"  Alamat untuk PC peserta : ws://{self.config.advertise_host or local_ip()}:{port}")
        print(f"  Kode pemasangan agent   : {self.pairing.code}   (6 digit)")
        print(f"  Basis data              : {self.config.db_path}")
        print("=" * 66)
        print("  Ketik kode di atas sekali saat memasang agent di PC lab.")
        print("  Kode dan pendaftaran PC baru bisa diatur di halaman Aturan.")
        print()

    def shutdown(self) -> None:
        """Stop the agent server cleanly, then close the database."""
        if self.loop.is_running():
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(self._stop_serving(), self.loop).result(
                    timeout=5
                )
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)
        with contextlib.suppress(Exception):
            self.db.close()

    async def _stop_serving(self) -> None:
        if self._beacon is not None:
            await self._beacon.stop()
            self._beacon = None
        if self._server is not None:
            await self._server.stop()
            self._server = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="labsentinel-dashboard")
    parser.add_argument("--config", type=Path, default=None, help="Berkas konfigurasi TOML.")
    parser.add_argument("--no-browser", action="store_true", help="Jangan buka jendela otomatis.")
    parser.add_argument(
        "--keep-running",
        action="store_true",
        help="Tetap berjalan meski jendela dashboard ditutup (disarankan saat ujian).",
    )
    args = parser.parse_args(argv)

    config = load_dashboard_config(args.config)
    setup_logging(log_path=config.log_path, level=config.log_level)

    app = DashboardApp(config)
    try:
        app.start_background()
    except Exception:
        log.exception("failed to start the agent server")
        print("Gagal menjalankan server untuk agent. Lihat berkas log untuk detail.")
        return 1

    try:
        app.run_ui(open_browser=not args.no_browser, keep_running=args.keep_running)
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
