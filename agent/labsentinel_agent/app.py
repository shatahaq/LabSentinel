"""Agent entry point: wires every part into one runnable program.

    python -m labsentinel_agent --pair 123456     # first install
    python -m labsentinel_agent                   # afterwards

What it starts, in order:

1. **Recovery** — restore any browser policy left behind by an abnormal exit,
   before touching anything else.
2. **Tray bridge** — so the participant can see monitoring status immediately.
3. **Enforcement** — safety gate, whitelist matcher, process detection (WMI
   events where available, polling otherwise).
4. **Transport** — find the dashboard by UDP broadcast (or a configured URL)
   and keep the connection alive.

Shutdown is deliberate about one thing: the browser policy is always restored,
so a stopped agent never leaves a lab PC with sites blocked and nobody watching.

This process does not draw on screen. The tray icon and warning popups belong to
the separate user-session helper (``python -m labsentinel_agent.tray``), because
a Windows Service in session 0 cannot reach the desktop — see ADR-0004.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import platform
import socket
import sys
import uuid
from pathlib import Path

from labsentinel_agent.capture.screenshot import MssScreenshotCapturer
from labsentinel_agent.capture.streamer import ScreenStreamer
from labsentinel_agent.core import AgentCore, RuntimeState, _Detector
from labsentinel_agent.enforce.detector import PollingProcessSource
from labsentinel_agent.enforce.engine import EnforcementEngine, ViolationRecord
from labsentinel_agent.enforce.identity import ProcessInfo
from labsentinel_agent.enforce.matcher import WhitelistMatcher
from labsentinel_agent.enforce.ports import ProcessController
from labsentinel_agent.enforce.safety import SafetyPolicy
from labsentinel_agent.enforce.title_watch import TitleHit, TitleWatcher
from labsentinel_agent.policy.browser_policy import BrowserPolicyManager
from labsentinel_agent.policy.registry import RegistryPort
from labsentinel_agent.rule_cache import RuleCache
from labsentinel_agent.transport.client import AgentIdentity, BackoffPolicy, DashboardClient
from labsentinel_agent.tray.bridge import TrayBridge
from labsentinel_shared.config import AgentConfig, load_agent_config
from labsentinel_shared.discovery import discover_dashboard
from labsentinel_shared.enums import AgentState
from labsentinel_shared.logging_setup import setup_logging
from labsentinel_shared.rules import RuleSet

log = logging.getLogger("labsentinel.agent.app")

AGENT_VERSION = "0.1.0"
_IS_WINDOWS = sys.platform == "win32"

# Process exit codes, so an installer or service wrapper can tell what happened.
EXIT_OK = 0
EXIT_NOT_PAIRED = 2
EXIT_PAIRING_REJECTED = 3

NOT_PAIRED_MESSAGE = (
    "\nPC ini belum dipasangkan.\n"
    "Jalankan sekali dengan kode 6 digit yang tampil di dashboard (halaman Aturan):\n"
    "    labsentinel-agent --pair KODE\n"
)


# ---------------------------------------------------------------- identity
def stable_agent_id(config: AgentConfig) -> str:
    """A stable id for this PC, created once and kept across restarts."""
    path = config.agent_id_path
    with contextlib.suppress(OSError):
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    agent_id = f"{socket.gethostname().lower()}-{uuid.uuid4().hex[:8]}"[:64]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(agent_id, encoding="utf-8")
    log.info("created agent id %s", agent_id)
    return agent_id


def read_token(config: AgentConfig) -> str | None:
    with contextlib.suppress(OSError):
        token = config.token_file.read_text(encoding="utf-8").strip()
        if token:
            return token
    return None


def store_token(config: AgentConfig, token: str) -> None:
    config.token_file.parent.mkdir(parents=True, exist_ok=True)
    config.token_file.write_text(token, encoding="utf-8")


# ---------------------------------------------------------------- the agent
class LabSentinelAgent:
    def __init__(self, config: AgentConfig, *, pairing_code: str | None = None) -> None:
        self.config = config
        self.pairing_code = pairing_code
        self.agent_id = stable_agent_id(config)
        self.runtime = RuntimeState(state=AgentState.ENFORCING)
        self.cache = RuleCache(config.rule_cache_path)

        self.tray = TrayBridge(
            token_file=config.data_dir / "tray_token",
            endpoint_file=config.data_dir / "tray_endpoint",
        )
        self.policy = BrowserPolicyManager(
            _registry(), backup_path=config.data_dir / "policy_backup.json"
        )
        self.capturer = MssScreenshotCapturer()
        self.streamer = ScreenStreamer(
            capturer=self.capturer,
            send=self._send_frame,
            state_provider=lambda: self.runtime.state,
        )

        safety = SafetyPolicy(own_pids=frozenset({_own_pid()}))
        self.detector_source = PollingProcessSource(
            _snapshot, interval_s=config.poll_fallback_interval_s
        )
        self.engine = EnforcementEngine(
            safety=safety,
            matcher=WhitelistMatcher(),
            controller=_controller(),
            warner=self.tray,  # warnings reach the participant via the tray helper
            capturer=self.capturer,
            emit=self._emit_violation,
            state_provider=lambda: self.runtime.state,
            ruleset_provider=self.runtime.effective_ruleset,
        )
        self.core = AgentCore(
            runtime=self.runtime,
            cache=self.cache,
            engine=self.engine,
            detector=_Detector(
                watch_new_processes=self.detector_source.watch_new_processes,
                list_processes=self.detector_source.list_processes,
                stop=self.detector_source.stop,
            ),
            lock_fn=_lock_screen,
            notify_fn=self.tray.notify,
            status_sink=lambda state, connected: self.tray.set_status(state, connected=connected),
            policy=self.policy,
            streamer=self.streamer,
        )
        self.titles = TitleWatcher(
            foreground=_foreground,
            ruleset_provider=self.runtime.effective_ruleset,
            enabled=lambda: self.runtime.state is AgentState.ENFORCING,
            on_hit=self._on_title_hit,
        )
        self.client: DashboardClient | None = None

    # ------------------------------------------------------------ lifecycle
    async def run(self) -> int:
        """Run until stopped or until pairing fails. Returns a process exit code."""
        # A leftover policy backup means the last run ended abnormally; put the
        # machine back the way we found it before doing anything else.
        if self.policy.recover_if_needed():
            log.warning("restored browser policy left behind by a previous run")

        cached = self.cache.load()
        if cached is not None:
            self.runtime.ruleset = cached
            log.info("resuming with cached rules (revision %d)", cached.revision)

        await self.tray.start()
        self.core.publish_status()

        # An explicit --pair always starts a fresh pairing, even with a token on
        # disk: that token may belong to a dashboard database that was reset,
        # and while we present it the dashboard would never look at the code.
        token = None if self.pairing_code else read_token(self.config)
        if token is None and not self.pairing_code:
            log.error(
                "this PC is not paired yet; run once with --pair <kode 6 digit> "
                "(the code is shown on the dashboard)"
            )
            print(NOT_PAIRED_MESSAGE)
            return EXIT_NOT_PAIRED

        self.client = DashboardClient(
            identity=AgentIdentity(
                agent_id=self.agent_id,
                hostname=socket.gethostname(),
                username=_current_user(),
                agent_version=AGENT_VERSION,
                os_version=platform.platform(terse=True),
            ),
            token=token,
            pairing_code=self.pairing_code,
            on_token_issued=self._on_token_issued,
            state_provider=self.core.state,
            metrics_provider=_metrics,
            on_ruleset=self.core.on_ruleset,
            on_command=self.core.on_command,
            cached_revision_provider=lambda: (
                self.cache.current.revision if self.cache.current else -1
            ),
            indicator_visible_provider=lambda: self.tray.indicator_visible,
            heartbeat_interval_s=self.config.heartbeat_interval_s,
            backoff=BackoffPolicy(),
        )
        self.core.attach_client(self.client)

        await self.core.sweep_running()

        link = asyncio.ensure_future(self.client.run(self._find_dashboard))
        tasks = [
            link,
            asyncio.ensure_future(self.core.run_detection()),
            asyncio.ensure_future(self.streamer.run()),
            asyncio.ensure_future(self.titles.run()),
            asyncio.ensure_future(self._watch_connection()),
        ]
        try:
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()  # surface a crash instead of hiding it
                if link in done:  # the link only ends when stopped or refused
                    break
        finally:
            for task in tasks:
                task.cancel()

        if self.client.pairing_rejected:
            print(
                f"\nPemasangan ditolak oleh dashboard: {self.client.pairing_rejected}.\n"
                "Periksa kode di halaman Aturan dashboard, lalu jalankan lagi:\n"
                "    labsentinel-agent --pair KODE\n"
            )
            return EXIT_PAIRING_REJECTED
        return EXIT_OK

    def _on_token_issued(self, token: str) -> None:
        store_token(self.config, token)
        log.info("this PC is now paired; token stored at %s", self.config.token_file)
        print("\nPC ini berhasil dipasangkan. Kode tidak perlu diketik lagi.\n")

    async def _watch_connection(self) -> None:
        """Keep the participant's indicator honest about connectivity."""
        last = None
        while True:
            connected = self.client is not None and self.client.connected.is_set()
            if connected != last:
                last = connected
                self.core.set_connected(connected)
            await asyncio.sleep(1.0)

    async def _find_dashboard(self) -> str | None:
        if self.config.direct_url:
            return self.config.direct_url
        beacon = await discover_dashboard(
            port=self.config.discovery_port, timeout_s=self.config.discovery_timeout_s
        )
        if beacon is None:
            log.info("no dashboard found on the network yet; will keep looking")
            return None
        log.info("found dashboard '%s' at %s", beacon.dashboard_name, beacon.ws_url)
        return beacon.ws_url

    async def shutdown(self) -> None:
        """Always hand the machine back in the state we found it.

        Each step is isolated: one part failing to stop must never keep the
        browser policy in place, or a lab PC would stay with sites blocked and
        nobody watching.
        """
        log.info("agent stopping")
        for step in (self.titles.stop, self.streamer.stop, self.core.stop):
            try:
                step()
            except Exception:
                log.exception("error while stopping %s", getattr(step, "__qualname__", step))
        try:
            self.policy.clear()
        except Exception:
            # The backup file is kept, so the next start restores it.
            log.exception("could not restore the browser policy now; will retry on next start")
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.stop()
        with contextlib.suppress(Exception):
            await self.tray.stop()

    # ------------------------------------------------------------- plumbing
    async def _emit_violation(self, record: ViolationRecord) -> None:
        await self.core.emit_violation(record)

    async def _on_title_hit(self, hit: TitleHit) -> None:
        """Record a suspected blocked site seen in the browser's title bar."""
        from labsentinel_shared.enums import ViolationKind

        ruleset: RuleSet | None = self.runtime.effective_ruleset()
        await self.core.emit_violation(
            ViolationRecord(
                kind=ViolationKind.SITE_TITLE,
                subject=hit.matched,
                detail=f"Judul jendela: {hit.title}",
                pid=0,
                dry_run=bool(ruleset.dry_run) if ruleset else True,
                acted=False,
                occurred_at=__import__("time").time(),
                screenshot_jpeg=self.capturer.capture_jpeg(max_width=960, quality=55),
            )
        )

    async def _send_frame(self, jpeg: bytes, width: int, height: int, hi_res: bool) -> bool:
        import base64
        import time as _time

        from labsentinel_shared.protocol import ScreenFramePayload

        if self.client is None:
            return False
        return await self.client.send_event(
            ScreenFramePayload(
                agent_id=self.agent_id,
                hi_res=hi_res,
                width=width,
                height=height,
                jpeg_b64=base64.b64encode(jpeg).decode("ascii"),
                captured_at=_time.time(),
            )
        )


# --------------------------------------------------- platform indirections
# Imported lazily so the module still loads on a non-Windows dev machine.
def _registry() -> RegistryPort:
    from labsentinel_agent.policy.registry import FakeRegistry, WinRegistry

    if _IS_WINDOWS:
        try:
            return WinRegistry()
        except Exception:
            log.warning("registry unavailable; site blocking disabled", exc_info=True)
    # Off Windows (or without access) policy writes go nowhere rather than
    # failing: application whitelisting still works.
    return FakeRegistry()


def _controller() -> ProcessController:
    from labsentinel_agent.platform.win_process import WinProcessController

    return WinProcessController()


def _snapshot() -> list[ProcessInfo]:
    from labsentinel_agent.platform.win_process import collect_processes

    return collect_processes(own_pids=frozenset({_own_pid()}))


def _foreground() -> tuple[str, str]:
    from labsentinel_agent.platform.win_window import foreground_process_and_title

    return foreground_process_and_title()


def _metrics() -> tuple[float, float, str]:
    from labsentinel_agent.platform.win_window import agent_metrics

    return agent_metrics()


def _lock_screen() -> bool:
    from labsentinel_agent.platform.win_window import lock_workstation

    return lock_workstation()


def _own_pid() -> int:
    import os

    return os.getpid()


def _current_user() -> str:
    import os

    try:
        return os.getlogin()
    except OSError:
        return os.environ.get("USERNAME", "")


# ------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="labsentinel-agent",
        description="Agent Lab Sentinel untuk PC peserta.",
    )
    parser.add_argument("--config", type=Path, default=None, help="Berkas konfigurasi TOML.")
    parser.add_argument(
        "--pair",
        metavar="KODE",
        default=None,
        help="Kode 6 digit dari dashboard; hanya diperlukan sekali saat pemasangan.",
    )
    parser.add_argument(
        "--dashboard", default=None, help="Alamat dashboard (lewati penemuan otomatis)."
    )
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR.")
    args = parser.parse_args(argv)

    config = load_agent_config(args.config)
    if args.dashboard:
        config = config.model_copy(update={"direct_url": args.dashboard})
    setup_logging(log_path=config.log_path, level=args.log_level or config.log_level)

    if not _IS_WINDOWS:
        log.warning("Lab Sentinel enforcement is Windows-only; running in a limited mode")

    agent = LabSentinelAgent(config, pairing_code=args.pair)

    async def runner() -> int:
        try:
            return await agent.run()
        finally:
            await agent.shutdown()

    try:
        return asyncio.run(runner())
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
