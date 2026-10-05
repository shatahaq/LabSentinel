"""Agent entry point: wires every part into one runnable program.

    python -m labsentinel_agent --pair 123456     # first install
    python -m labsentinel_agent                   # afterwards

What it starts, in order:

1. **Recovery** — restore any browser policy left behind by an abnormal exit,
   before touching anything else.
2. **Tray bridge** — so the participant can see monitoring status immediately.
3. **Enforcement** — safety gate, whitelist matcher, process detection by
   polling (every ``poll_fallback_interval_s``, default 3 s). The WMI event
   source in platform/win_detector.py exists but is not wired in.
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
import signal
import socket
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from labsentinel_agent.capture.streamer import ScreenStreamer
from labsentinel_agent.core import AgentCore, RuntimeState, _Detector
from labsentinel_agent.desktop.access import DesktopAccess, DesktopCapturer
from labsentinel_agent.desktop.proxy import DesktopProxy
from labsentinel_agent.enforce.detector import PollingProcessSource
from labsentinel_agent.enforce.engine import EnforcementEngine, ViolationRecord
from labsentinel_agent.enforce.identity import ProcessInfo
from labsentinel_agent.enforce.matcher import WhitelistMatcher
from labsentinel_agent.enforce.ports import ProcessController
from labsentinel_agent.enforce.safety import SafetyPolicy
from labsentinel_agent.enforce.title_watch import TitleHit, TitleWatcher
from labsentinel_agent.lifecycle import (
    InstanceLock,
    LoopWatchdog,
    PreviousRun,
    RunMarker,
    exit_for_restart,
)
from labsentinel_agent.policy.browser_policy import BrowserPolicyManager
from labsentinel_agent.policy.registry import RegistryPort
from labsentinel_agent.rule_cache import RuleCache
from labsentinel_agent.transport.client import (
    AgentIdentity,
    BackoffPolicy,
    CommandResult,
    DashboardClient,
)
from labsentinel_agent.tray.bridge import TrayBridge
from labsentinel_shared.config import AgentConfig, load_agent_config
from labsentinel_shared.discovery import discover_dashboard
from labsentinel_shared.enums import AgentState, ViolationKind
from labsentinel_shared.logging_setup import setup_logging
from labsentinel_shared.rules import RuleSet

log = logging.getLogger("labsentinel.agent.app")

AGENT_VERSION = "0.1.0"
_IS_WINDOWS = sys.platform == "win32"

# Process exit codes, so an installer or service wrapper can tell what happened.
EXIT_OK = 0
EXIT_NOT_PAIRED = 2
EXIT_PAIRING_REJECTED = 3
EXIT_ALREADY_RUNNING = 4
EXIT_NO_DASHBOARD = 5

PAIR_ONLY_TIMEOUT_S = 60.0

MARKER_TOUCH_S = 15.0
TRAY_GRACE_S = 20.0  # indicator missing this long in a session: start the helper
TRAY_RESPAWN_S = 30.0

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
    """Persist this PC's credential atomically and readable by its owner only.

    mkstemp creates the file 0600 on POSIX. On Windows mode bits do not
    apply, so the file's ACL is set to SYSTEM and Administrators only.
    """
    import os
    import tempfile

    from labsentinel_agent.platform.acl import restrict_to_admins

    path = config.token_file
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".token-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    restrict_to_admins(path)  # Windows: ACL, since mode bits do not apply


# ---------------------------------------------------------------- the agent
class LabSentinelAgent:
    def __init__(
        self,
        config: AgentConfig,
        *,
        pairing_code: str | None = None,
        pair_only: bool = False,
        service_mode: bool = False,
        config_path: Path | None = None,
    ) -> None:
        self.config = config
        self.pairing_code = pairing_code
        self.pair_only = pair_only
        # As a Windows Service (session 0) the desktop is reached through a
        # worker in the user's session; from a console, directly. ADR-0006.
        self.service_mode = service_mode
        self.config_path = config_path
        self.desktop_proxy = (
            DesktopProxy(
                endpoint_file=config.data_dir / "desktop_endpoint",
                token_file=config.data_dir / "desktop_token",
            )
            if service_mode
            else None
        )
        self.desktop = DesktopAccess(
            proxy=self.desktop_proxy, local=_local_desktop(), service_mode=service_mode
        )
        self._someone_logged_in: Callable[[], bool] = lambda: True
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
        self.capturer = DesktopCapturer(self.desktop)
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
            controller=_controller(self.desktop.close_windows),
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
            lock_fn=self.desktop.lock,
            notify_fn=self.tray.notify,
            status_sink=lambda state, connected: self.tray.set_status(state, connected=connected),
            policy=self.policy,
            streamer=self.streamer,
        )
        self.titles = TitleWatcher(
            foreground=self.desktop.foreground,
            ruleset_provider=self.runtime.effective_ruleset,
            enabled=lambda: self.runtime.state is AgentState.ENFORCING,
            on_hit=self._on_title_hit,
        )
        self.client: DashboardClient | None = None

        # Anti-tamper (see lifecycle.py): one instance per PC, a marker that
        # reveals an abnormal end of the previous run, and a loop watchdog.
        self.instance_lock = InstanceLock(config.data_dir / "agent.lock")
        self.marker = RunMarker(config.data_dir / "running.json")
        self.watchdog = LoopWatchdog(on_stall=self._on_stall)
        self.previous_run: PreviousRun | None = None
        # Why we are stopping, kept for the next start ("" = a normal stop).
        self.stop_reason = ""
        self._another_instance = False
        self._started = False

    # ------------------------------------------------------------ lifecycle
    async def run(self) -> int:
        """Run until stopped or until pairing fails. Returns a process exit code."""
        if not self.instance_lock.acquire():
            # Two agents with one identity would keep replacing each other's
            # connection on the dashboard. Leave the running one alone.
            self._another_instance = True
            log.error("another Lab Sentinel agent is already running on this PC")
            print("\nAgent Lab Sentinel sudah berjalan di PC ini.\n")
            return EXIT_ALREADY_RUNNING
        if self.pair_only:
            return await self._pair_only()
        self._started = True
        _set_window_source(self.desktop.window_index)
        if self.desktop_proxy is not None:
            self.desktop_proxy.start()

        # A leftover policy backup means the last run ended abnormally; put the
        # machine back the way we found it before doing anything else.
        if self.policy.recover_if_needed():
            log.warning("restored browser policy left behind by a previous run")
        self.previous_run = self.marker.begin()
        if self.previous_run is not None:
            log.warning("previous run did not end normally: %s", self.previous_run.describe())

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
            identity=self._identity(),
            token=token,
            pairing_code=self.pairing_code,
            on_token_issued=self._on_token_issued,
            state_provider=self.core.state,
            metrics_provider=self._metrics,
            on_ruleset=self.core.on_ruleset,
            on_command=self.core.on_command,
            cached_revision_provider=lambda: (
                self.cache.current.revision if self.cache.current else -1
            ),
            indicator_visible_provider=lambda: self.tray.indicator_visible,
            desktop_access_provider=lambda: self.desktop.available(
                someone_logged_in=self._someone_logged_in()
            ),
            heartbeat_interval_s=self.config.heartbeat_interval_s,
            backoff=BackoffPolicy(),
        )
        self.core.attach_client(self.client)

        await self.core.sweep_running()

        self.watchdog.start()
        extra: list[asyncio.Future[Any]] = []
        if self.desktop_proxy is not None:
            extra.append(asyncio.ensure_future(self._supervise_desktop_worker()))
        link = asyncio.ensure_future(self.client.run(self._find_dashboard))
        tasks: list[asyncio.Future[Any]] = [
            link,
            asyncio.ensure_future(self.core.run_detection()),
            asyncio.ensure_future(self.streamer.run()),
            asyncio.ensure_future(self.titles.run()),
            asyncio.ensure_future(self._watch_connection()),
            asyncio.ensure_future(self.watchdog.run_beats()),
            asyncio.ensure_future(self._keep_marker_fresh()),
        ]
        if self.previous_run is not None:
            tasks.append(asyncio.ensure_future(self._report_previous_run(self.previous_run)))
        tasks.extend(extra)
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

    def _identity(self) -> AgentIdentity:
        return AgentIdentity(
            agent_id=self.agent_id,
            hostname=socket.gethostname(),
            username=_current_user(),
            agent_version=AGENT_VERSION,
            os_version=platform.platform(terse=True),
        )

    async def _pair_only(self) -> int:
        """Pair, store the token, and exit. Used by the installer.

        Nothing is enforced and no rules are applied: this is the one-off step
        before the service is installed, typically run by an administrator.
        """
        if not self.pairing_code:
            print(NOT_PAIRED_MESSAGE)
            return EXIT_NOT_PAIRED
        issued = asyncio.Event()

        def on_issued(token: str) -> None:
            self._on_token_issued(token)
            issued.set()

        async def ignore_rules(_ruleset: RuleSet) -> None:
            return None

        async def refuse_commands(_cmd: Any) -> CommandResult:
            return CommandResult(resulting_state=AgentState.PAUSED, ok=False,
                                 message="sedang dipasangkan")

        client = DashboardClient(
            identity=self._identity(), token=None, pairing_code=self.pairing_code,
            on_token_issued=on_issued, state_provider=lambda: AgentState.PAUSED,
            metrics_provider=lambda: (0.0, 0.0, ""), on_ruleset=ignore_rules,
            on_command=refuse_commands, backoff=BackoffPolicy(base_s=1.0, max_s=5.0),
        )
        self.client = client
        link = asyncio.ensure_future(client.run(self._find_dashboard))
        waiter = asyncio.ensure_future(issued.wait())
        try:
            await asyncio.wait(
                {link, waiter}, timeout=PAIR_ONLY_TIMEOUT_S,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            waiter.cancel()
            await client.stop()
            link.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await link
        if issued.is_set():
            return EXIT_OK
        if client.pairing_rejected:
            print(f"\nPemasangan ditolak oleh dashboard: {client.pairing_rejected}.\n")
            return EXIT_PAIRING_REJECTED
        print(
            "\nDashboard tidak ditemukan di jaringan. Pastikan dashboard berjalan, "
            "atau sebutkan alamatnya dengan --dashboard ws://ALAMAT:8765\n"
        )
        return EXIT_NO_DASHBOARD

    def _on_token_issued(self, token: str) -> None:
        store_token(self.config, token)
        log.info("this PC is now paired; token stored at %s", self.config.token_file)
        print("\nPC ini berhasil dipasangkan. Kode tidak perlu diketik lagi.\n")

    async def _supervise_desktop_worker(self) -> None:
        """Service mode: keep a desktop worker in the active console session."""
        from labsentinel_agent import autostart
        from labsentinel_agent.desktop.spawn import (
            WorkerSupervisor,
            active_console_session,
            spawn_as_user,
            spawn_in_session,
            worker_command,
        )

        proxy = self.desktop_proxy
        assert proxy is not None
        command = worker_command(self.config_path)
        supervisor = WorkerSupervisor(
            active_session=active_console_session,
            connected_session=lambda: proxy.session_id,
            spawn=lambda session: spawn_in_session(session, command),
        )
        # Transparency: if the participant's indicator is gone for a while
        # (helper closed or killed), start it again as that participant. The
        # grace period lets the login Run key start it first; the helper
        # itself refuses to run twice for one user.
        tray_command = autostart.tray_command(data_dir=self.config.data_dir)
        tray_supervisor = WorkerSupervisor(
            active_session=active_console_session,
            connected_session=lambda: (
                active_console_session() if self.tray.indicator_visible else None
            ),
            spawn=lambda session: spawn_as_user(session, tray_command),
            grace_s=TRAY_GRACE_S,
            respawn_after_s=TRAY_RESPAWN_S,
        )
        self._someone_logged_in = supervisor.someone_logged_in
        await asyncio.gather(
            supervisor.run(), tray_supervisor.run(every_s=5.0),
            self._sweep_when_desktop_arrives(),
        )

    async def _sweep_when_desktop_arrives(self) -> None:
        """Re-check running apps whenever a desktop worker (re)connects.

        Until then nothing on the desktop is visible, so apps already open at
        that moment (service restarted mid-session, user just logged in) would
        otherwise only be checked when something changes.
        """
        proxy = self.desktop_proxy
        assert proxy is not None
        seen = None
        while True:
            current = proxy.session_id if proxy.connected else None
            if current is not None and current != seen:
                log.info("desktop worker ready for session %s; checking running apps", current)
                await self.core.sweep_running()
            seen = current
            await asyncio.sleep(1.0)

    def _metrics(self) -> tuple[float, float, str]:
        cpu, mem, _local_title = _metrics()
        return cpu, mem, self.desktop.foreground()[1]

    def _on_stall(self, age: float) -> None:
        """The loop is hung: note why, then exit so the service restarts us."""
        with contextlib.suppress(Exception):
            self.marker.end(reason="watchdog")  # not a kill; say so next start
        exit_for_restart(age)

    async def _keep_marker_fresh(self) -> None:
        """Note that we are alive, so an abnormal end can be dated."""
        while True:
            await asyncio.sleep(MARKER_TOUCH_S)
            self.marker.touch()

    async def _report_previous_run(self, previous: PreviousRun) -> None:
        """Tell the dashboard the agent was stopped abnormally, once connected."""
        record = ViolationRecord(
            kind=ViolationKind.AGENT_TAMPER,
            subject="Agent Lab Sentinel",
            detail=previous.describe(),
            pid=previous.pid,
            dry_run=False,  # a fact about the agent, not an enforcement action
            acted=False,
            occurred_at=previous.last_alive or time.time(),
        )
        while True:
            if self.client is not None:
                await self.client.connected.wait()
            if await self.core.emit_violation(record):
                log.info("reported the abnormal end of the previous run")
                return
            await asyncio.sleep(2.0)

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
        if self._another_instance:
            return  # the PC belongs to the instance that is already running
        log.info("agent stopping")
        self.watchdog.stop()
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
        if self._started:
            # Removed on a normal stop; kept with a reason (e.g. the service
            # was stopped by hand) so the next start can report it.
            self.marker.end(reason=self.stop_reason)
        if self.desktop_proxy is not None:
            with contextlib.suppress(Exception):
                self.desktop_proxy.stop()  # the worker is told to exit
        _set_window_source(None)
        self.instance_lock.release()

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


def _controller(window_closer: Callable[[int], bool]) -> ProcessController:
    from labsentinel_agent.platform.win_process import WinProcessController

    return WinProcessController(window_closer=window_closer)


def _local_desktop() -> Any:
    from labsentinel_agent.desktop.worker import LocalDesktopOps

    return LocalDesktopOps()


def _set_window_source(provider: Callable[[], dict[int, str] | None] | None) -> None:
    from labsentinel_agent.platform.win_process import set_window_index_provider

    set_window_index_provider(provider)


def _snapshot() -> list[ProcessInfo]:
    from labsentinel_agent.platform.win_process import collect_processes

    return collect_processes(own_pids=frozenset({_own_pid()}))


def _metrics() -> tuple[float, float, str]:
    from labsentinel_agent.platform.win_window import agent_metrics

    return agent_metrics()


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
def default_config_path() -> Path | None:
    """agent.toml next to an installed executable, if there is one.

    The service, the tray helper and the command line all read the same file,
    so an installation is configured in exactly one place.
    """
    if getattr(sys, "frozen", False):
        candidate = Path(sys.executable).with_name("agent.toml")
        if candidate.exists():
            return candidate
    return None


def _stop_on_signals(task: asyncio.Task[Any] | None) -> None:
    """Turn termination signals into a normal shutdown (policy restored).

    Ctrl+C already arrives as KeyboardInterrupt. This covers ``kill`` on POSIX
    and Ctrl+Break / console close on Windows. Under the Windows Service the
    service control handler does this instead (service.py).
    """
    if task is None:
        return
    loop = asyncio.get_running_loop()
    if sys.platform == "win32":
        def _handler(_signum: int, _frame: object) -> None:
            loop.call_soon_threadsafe(task.cancel)

        with contextlib.suppress(ValueError, OSError, AttributeError):
            signal.signal(signal.SIGBREAK, _handler)
        return
    with contextlib.suppress(NotImplementedError, RuntimeError):
        loop.add_signal_handler(signal.SIGTERM, task.cancel)


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
        "--pair-only",
        action="store_true",
        help="Hanya pasangkan PC ini lalu keluar (dipakai installer sebelum memasang layanan).",
    )
    parser.add_argument(
        "--dashboard", default=None, help="Alamat dashboard (lewati penemuan otomatis)."
    )
    parser.add_argument(
        "--desktop-worker",
        action="store_true",
        help=argparse.SUPPRESS,  # started by the service inside the user's session
    )
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR.")
    args = parser.parse_args(argv)

    config = load_agent_config(args.config or default_config_path())
    if args.desktop_worker:
        from labsentinel_agent.desktop.worker import run_worker

        setup_logging(
            log_path=config.data_dir / "logs" / "desktop-worker.log",
            level=args.log_level or config.log_level, console=False,
        )
        return run_worker(config.data_dir)
    if args.dashboard:
        config = config.model_copy(update={"direct_url": args.dashboard})
    setup_logging(log_path=config.log_path, level=args.log_level or config.log_level)

    if not _IS_WINDOWS:
        log.warning("Lab Sentinel enforcement is Windows-only; running in a limited mode")

    if args.pair_only and not args.pair and sys.stdin and sys.stdin.isatty():
        # "Pasangkan ulang PC ini" from a console: ask instead of failing.
        with contextlib.suppress(EOFError, KeyboardInterrupt):
            args.pair = input("Kode pemasangan 6 digit dari dashboard: ").strip() or None
    agent = LabSentinelAgent(config, pairing_code=args.pair, pair_only=args.pair_only)

    async def runner() -> int:
        _stop_on_signals(asyncio.current_task())
        try:
            return await agent.run()
        except asyncio.CancelledError:
            return EXIT_OK  # asked to stop (SIGTERM / Ctrl+Break)
        finally:
            await agent.shutdown()

    try:
        return asyncio.run(runner())
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
