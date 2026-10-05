"""Desktop access for a service: the proxy (service side) and the worker
(user-session side) over a real loopback socket, plus the supervisor's
decisions. The Windows calls themselves (CreateProcessAsUser, EnumWindows,
mss in a session) are replaced by fakes here and checked by hand on Windows.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from labsentinel_agent.desktop.access import DesktopAccess, DesktopCapturer
from labsentinel_agent.desktop.proxy import DesktopProxy
from labsentinel_agent.desktop.spawn import NO_SESSION, WorkerSupervisor, worker_command
from labsentinel_agent.desktop.worker import DesktopWorker
from labsentinel_agent.platform import win_process

pytestmark = pytest.mark.integration


class FakeDesktop:
    """What the worker would see inside session 3."""

    def __init__(self, *, session: int = 3, slow_s: float = 0.0) -> None:
        self.session = session
        self.slow_s = slow_s
        self.closed: list[int] = []
        self.locked = 0

    def window_index(self) -> dict[int, str]:
        time.sleep(self.slow_s)
        return {4242: "Steam", 5151: "Visual Studio Code"}

    def foreground(self) -> tuple[str, str]:
        return "chrome.exe", "ChatGPT - Google Chrome"

    def capture_jpeg(self, *, max_width: int, quality: int) -> bytes | None:
        return b"\xff\xd8JPEG" + bytes([max_width % 256, quality])

    def close_windows(self, pid: int) -> bool:
        self.closed.append(pid)
        return True

    def lock(self) -> bool:
        self.locked += 1
        return True

    def session_id(self) -> int:
        return self.session


class LocalStub:
    """The session-0 view: nothing useful (what a service sees on its own)."""

    def window_index(self) -> dict[int, str]:
        return {}

    def foreground(self) -> tuple[str, str]:
        return "local.exe", "local title"

    def capture_jpeg(self, *, max_width: int, quality: int) -> bytes | None:
        return b"local"

    def close_windows(self, pid: int) -> bool:
        return True

    def lock(self) -> bool:
        return True


@pytest.fixture
def proxy(tmp_path: Path) -> Iterator[DesktopProxy]:
    p = DesktopProxy(endpoint_file=tmp_path / "desktop_endpoint",
                     token_file=tmp_path / "desktop_token")
    p.start()
    yield p
    p.stop()


@contextlib.asynccontextmanager
async def _worker(tmp_path: Path, desktop: FakeDesktop) -> AsyncIterator[DesktopWorker]:
    worker = DesktopWorker(ops=desktop, endpoint_file=tmp_path / "desktop_endpoint",
                           token_file=tmp_path / "desktop_token", give_up_after_s=5)
    task = asyncio.ensure_future(worker.run())
    try:
        yield worker
    finally:
        worker.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=5)


async def _until(cond: object, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():  # type: ignore[operator]
        if time.monotonic() > deadline:
            raise TimeoutError
        await asyncio.sleep(0.02)


def _ask(fn: object, *args: object, **kwargs: object) -> object:
    """Call a blocking proxy method from a plain thread (as the agent does)."""
    return asyncio.to_thread(fn, *args, **kwargs)  # type: ignore[arg-type]


async def test_worker_answers_every_desktop_question(proxy: DesktopProxy, tmp_path: Path) -> None:
    desktop = FakeDesktop()
    async with _worker(tmp_path, desktop):
        await _until(lambda: proxy.connected)
        assert proxy.session_id == 3
        assert await _ask(proxy.window_index) == {4242: "Steam", 5151: "Visual Studio Code"}
        assert await _ask(proxy.foreground) == ("chrome.exe", "ChatGPT - Google Chrome")
        jpeg = await _ask(proxy.capture_jpeg, max_width=960, quality=55)
        assert jpeg == b"\xff\xd8JPEG" + bytes([960 % 256, 55])
        assert await _ask(proxy.close_windows, 4242) is True
        assert await _ask(proxy.lock) is True
    assert desktop.closed == [4242] and desktop.locked == 1


async def test_blocking_call_from_the_agents_own_loop_does_not_deadlock(
    proxy: DesktopProxy, tmp_path: Path
) -> None:
    """Enforcement asks synchronously from the main loop; the proxy lives on
    its own loop so this returns instead of hanging. (The worker runs on its
    own thread here, as it is its own process in production.)"""
    worker = DesktopWorker(ops=FakeDesktop(), endpoint_file=tmp_path / "desktop_endpoint",
                           token_file=tmp_path / "desktop_token", give_up_after_s=5)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=lambda: loop.run_until_complete(worker.run()), daemon=True)
    thread.start()
    try:
        await _until(lambda: proxy.connected)
        started = time.monotonic()
        assert proxy.foreground() == ("chrome.exe", "ChatGPT - Google Chrome")  # this loop
        assert time.monotonic() - started < 1.0
    finally:
        loop.call_soon_threadsafe(worker.stop)
        thread.join(timeout=5)


async def test_many_threads_at_once(proxy: DesktopProxy, tmp_path: Path) -> None:
    async with _worker(tmp_path, FakeDesktop()):
        await _until(lambda: proxy.connected)
        results: list[object] = []

        def ask() -> None:
            results.append(proxy.window_index())

        threads = [threading.Thread(target=ask) for _ in range(12)]
        await asyncio.to_thread(lambda: [t.start() for t in threads])
        await asyncio.to_thread(lambda: [t.join(5) for t in threads])
        assert len(results) == 12 and all(r == {4242: "Steam", 5151: "Visual Studio Code"}
                                          for r in results)


async def test_a_hung_worker_times_out_instead_of_freezing_the_agent(
    proxy: DesktopProxy, tmp_path: Path
) -> None:
    async with _worker(tmp_path, FakeDesktop(slow_s=3.0)):
        await _until(lambda: proxy.connected)
        started = time.monotonic()
        assert await _ask(proxy.window_index) is None  # 1.5 s budget
        assert time.monotonic() - started < 3.0


async def test_wrong_token_is_refused(proxy: DesktopProxy, tmp_path: Path) -> None:
    """A participant cannot stand in for the worker with fake screenshots."""
    (tmp_path / "fake_token").write_text("guessed", encoding="utf-8")
    impostor = DesktopWorker(ops=FakeDesktop(), endpoint_file=tmp_path / "desktop_endpoint",
                             token_file=tmp_path / "fake_token", give_up_after_s=0.5)
    reason = await asyncio.wait_for(impostor.run(), timeout=10)
    assert reason == "service unreachable"
    assert not proxy.connected


async def test_token_file_is_private(proxy: DesktopProxy, tmp_path: Path) -> None:
    import stat
    import sys

    if sys.platform == "win32":
        pytest.skip("ACL checked by hand on Windows")
    assert stat.S_IMODE((tmp_path / "desktop_token").stat().st_mode) == 0o600


async def test_newer_worker_replaces_the_old_one_which_exits(
    proxy: DesktopProxy, tmp_path: Path
) -> None:
    """Fast user switching: the worker for the new session takes over; the old
    one is told to leave instead of fighting for the connection."""
    async with _worker(tmp_path, FakeDesktop(session=3)) as old:
        await _until(lambda: proxy.session_id == 3)
        async with _worker(tmp_path, FakeDesktop(session=5)):
            await _until(lambda: proxy.session_id == 5)
            await _until(lambda: old.exit_reason == "replaced")
            await asyncio.sleep(0.3)
            assert proxy.session_id == 5


async def test_worker_exits_when_the_service_stops(tmp_path: Path) -> None:
    proxy = DesktopProxy(endpoint_file=tmp_path / "desktop_endpoint",
                         token_file=tmp_path / "desktop_token")
    proxy.start()
    async with _worker(tmp_path, FakeDesktop()) as worker:
        await _until(lambda: proxy.connected)
        await asyncio.to_thread(proxy.stop)
        await _until(lambda: worker.exit_reason == "stopping")


async def test_worker_reconnects_after_a_dropped_link(proxy: DesktopProxy, tmp_path: Path) -> None:
    async with _worker(tmp_path, FakeDesktop()):
        await _until(lambda: proxy.connected)
        writer = proxy._writer
        assert writer is not None
        proxy._loop.call_soon_threadsafe(writer.transport.abort)  # type: ignore[union-attr]
        await _until(lambda: not proxy.connected)
        await _until(lambda: proxy.connected)


# ---------------------------------------------------- DesktopAccess choices
async def test_service_uses_the_worker_and_never_its_own_session(
    proxy: DesktopProxy, tmp_path: Path
) -> None:
    access = DesktopAccess(proxy=proxy, local=LocalStub(), service_mode=True)
    # No worker yet: nothing is visible, and that is said, not guessed.
    assert access.window_index() == {}
    assert access.capture_jpeg() is None
    assert access.foreground() == ("", "")
    assert access.close_windows(1) is False
    assert not access.available(someone_logged_in=True)
    assert access.available(someone_logged_in=False)  # nobody there: nothing to miss

    async with _worker(tmp_path, FakeDesktop()):
        await _until(lambda: proxy.connected)
        assert access.available()
        assert await asyncio.to_thread(access.window_index) == {
            4242: "Steam", 5151: "Visual Studio Code"}
        assert await asyncio.to_thread(DesktopCapturer(access).capture_jpeg)


def test_console_mode_uses_local_apis() -> None:
    access = DesktopAccess(proxy=None, local=LocalStub(), service_mode=False)
    assert access.window_index() is None  # platform layer enumerates locally
    assert access.foreground() == ("local.exe", "local title")
    assert access.capture_jpeg() == b"local"
    assert access.available()


def test_window_index_provider_feeds_the_safety_gate() -> None:
    win_process.set_window_index_provider(lambda: {77: "Game"})
    try:
        assert win_process.build_window_index() == {77: "Game"}
        win_process.set_window_index_provider(lambda: None)  # "ask locally"
        assert win_process.build_window_index() == win_process.local_window_index()
    finally:
        win_process.set_window_index_provider(None)


# --------------------------------------------------------- WorkerSupervisor
class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_supervisor_starts_a_worker_for_the_console_session() -> None:
    spawned: list[int] = []
    connected: list[int | None] = [None]
    clock = Clock()
    sup = WorkerSupervisor(active_session=lambda: 2, connected_session=lambda: connected[0],
                           spawn=lambda s: spawned.append(s) is None, clock=clock)
    assert sup.tick() and spawned == [2]
    assert not sup.tick()  # starting up: do not pile on
    clock.now = 20.0
    assert sup.tick() and spawned == [2, 2]  # never connected: try again
    connected[0] = 2
    clock.now = 40.0
    assert not sup.tick()  # serving: leave it


def test_supervisor_follows_a_session_switch() -> None:
    spawned: list[int] = []
    active = [2]
    sup = WorkerSupervisor(active_session=lambda: active[0], connected_session=lambda: 2,
                           spawn=lambda s: spawned.append(s) is None, clock=Clock())
    assert not sup.tick()
    active[0] = 4  # another user at the console
    assert sup.tick() and spawned == [4]


def test_supervisor_grace_lets_the_login_entry_start_it_first() -> None:
    spawned: list[int] = []
    visible = [False]
    clock = Clock()
    sup = WorkerSupervisor(
        active_session=lambda: 2, connected_session=lambda: 2 if visible[0] else None,
        spawn=lambda s: spawned.append(s) is None, clock=clock, grace_s=20.0,
    )
    assert not sup.tick()  # just logged in: give the Run key a chance
    clock.now = 10.0
    visible[0] = True  # the login entry started it
    assert not sup.tick()
    visible[0] = False  # participant kills the indicator
    clock.now = 15.0
    assert not sup.tick()
    clock.now = 36.0  # missing for over 20 s
    assert sup.tick() and spawned == [2]


@pytest.mark.parametrize("session", [NO_SESSION, 0, -1])
def test_supervisor_waits_while_nobody_is_at_the_console(session: int) -> None:
    sup = WorkerSupervisor(active_session=lambda: session, connected_session=lambda: None,
                           spawn=lambda s: pytest.fail("must not spawn"), clock=Clock())
    assert not sup.tick()
    assert not sup.someone_logged_in()


def test_worker_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:/Program Files/LS/labsentinel-service.exe")
    cmd = worker_command(Path("C:/Program Files/LS/agent.toml"))
    assert "labsentinel-agent.exe" in cmd and "--desktop-worker" in cmd
    assert '--config "' in cmd


# ------------------------------------- the whole agent in service mode
async def test_service_agent_sees_the_desktop_only_through_its_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import labsentinel_agent.app as app_mod
    import labsentinel_agent.desktop.spawn as spawn_mod
    from labsentinel_agent.policy.registry import FakeRegistry
    from labsentinel_dashboard.domain.pairing import PairingService
    from labsentinel_dashboard.storage.db import Database
    from labsentinel_dashboard.transport.server import WebSocketServer
    from labsentinel_shared.config import AgentConfig
    from labsentinel_shared.protocol import HeartbeatPayload
    from labsentinel_shared.rules import RuleSet

    monkeypatch.setattr(app_mod, "_registry", FakeRegistry)
    heartbeats: list[HeartbeatPayload] = []

    class Events:
        async def on_agent_connected(self, session: object) -> None: ...
        async def on_agent_disconnected(self, agent_id: str, *, clean: bool) -> None: ...
        async def on_heartbeat(self, agent_id: str, hb: HeartbeatPayload) -> None:
            heartbeats.append(hb)
        async def on_violation(self, agent_id: str, v: object) -> None: ...
        async def on_screen_frame(self, agent_id: str, f: object) -> None: ...
        async def on_ack(self, agent_id: str, ack: object) -> None: ...

    db = Database(tmp_path / "dash.sqlite")
    pairing = PairingService(db)
    server = WebSocketServer(host="127.0.0.1", port=0, authenticator=pairing,
                             ruleset_provider=lambda h: RuleSet(revision=1, dry_run=True),
                             events=Events(), heartbeat_interval_s=0.2)
    await server.start()

    data = tmp_path / "agent"
    config = AgentConfig(data_dir=data, token_file=data / "token",
                         direct_url=f"ws://127.0.0.1:{server.bound_port}")
    token = pairing.pair(agent_id=app_mod.stable_agent_id(config), hostname="LAB",
                         code=pairing.code, source="10.0.0.1").token
    assert token is not None
    app_mod.store_token(config, token)

    # Someone is at the console (session 3). "Spawning" starts a worker thread
    # with a fake desktop, standing in for CreateProcessAsUser on Windows.
    workers: list[tuple[asyncio.AbstractEventLoop, DesktopWorker, threading.Thread]] = []
    allow_spawn = threading.Event()

    def fake_spawn(session: int, command: str) -> bool:
        if not allow_spawn.is_set():
            return False
        w = DesktopWorker(ops=FakeDesktop(session=session), give_up_after_s=5,
                          endpoint_file=data / "desktop_endpoint",
                          token_file=data / "desktop_token")
        loop = asyncio.new_event_loop()
        t = threading.Thread(target=lambda: loop.run_until_complete(w.run()), daemon=True)
        t.start()
        workers.append((loop, w, t))
        return True

    monkeypatch.setattr(spawn_mod, "active_console_session", lambda: 3)
    monkeypatch.setattr(spawn_mod, "spawn_in_session", fake_spawn)
    monkeypatch.setattr(spawn_mod, "RESPAWN_AFTER_S", 0.0)

    agent = app_mod.LabSentinelAgent(config, service_mode=True)
    task = asyncio.ensure_future(agent.run())
    try:
        # No worker yet: the dashboard is told the desktop is out of reach.
        await _until(lambda: any(hb.desktop_access is False for hb in heartbeats), timeout=10)
        assert win_process.build_window_index() == {}  # session 0 sees nothing

        allow_spawn.set()
        await _until(lambda: heartbeats[-1].desktop_access is True, timeout=15)
        await _until(lambda: heartbeats[-1].active_window_title == "ChatGPT - Google Chrome",
                     timeout=10)
        # The safety gate now gets the user's windows, and evidence comes
        # from the user's screen.
        index = await asyncio.to_thread(win_process.build_window_index)
        assert index == {4242: "Steam", 5151: "Visual Studio Code"}
        shot = await asyncio.to_thread(agent.capturer.capture_jpeg, max_width=640, quality=50)
        assert shot is not None and shot.startswith(b"\xff\xd8")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await agent.shutdown()
        for loop, w, t in workers:
            loop.call_soon_threadsafe(w.stop)
            t.join(timeout=5)
        await server.stop()
        db.close()
    # Stopping the service tells the worker to leave.
    assert workers and workers[-1][1].exit_reason in {"stopping", "stopped"}
    assert win_process._window_index_provider is None
