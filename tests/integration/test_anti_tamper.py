"""Anti-tamper end to end: real agent processes, killed and restarted.

The agent runs as a separate OS process (``python -m labsentinel_agent``)
against a real dashboard server, then is killed the way Task Manager's "End
task" does it. The next start must report that to the dashboard. A normal stop
(SIGTERM here; the service stop on Windows) must not be reported as a kill.

Off Windows the enforcement layer protects every process, so running the real
agent here touches nothing on this machine.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import pytest

import labsentinel_agent.app as app_mod
from labsentinel_agent.app import EXIT_ALREADY_RUNNING, LabSentinelAgent, store_token
from labsentinel_agent.policy.registry import FakeRegistry
from labsentinel_agent.service import ServiceRunner
from labsentinel_dashboard.domain.pairing import PairingService
from labsentinel_dashboard.storage.db import Database
from labsentinel_dashboard.transport.server import AgentSession, WebSocketServer
from labsentinel_shared.config import AgentConfig
from labsentinel_shared.enums import ViolationKind
from labsentinel_shared.protocol import (
    AckPayload,
    HeartbeatPayload,
    ScreenFramePayload,
    ViolationPayload,
)
from labsentinel_shared.rules import RuleSet

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX signals"),
]

ROOT = Path(__file__).resolve().parents[2]


class Events:
    def __init__(self) -> None:
        self.connected: list[str] = []
        self.violations: list[ViolationPayload] = []
        self._changed = asyncio.Event()

    async def on_agent_connected(self, session: AgentSession) -> None:
        self.connected.append(session.agent_id)
        self._changed.set()

    async def on_agent_disconnected(self, agent_id: str, *, clean: bool) -> None:
        self._changed.set()

    async def on_heartbeat(self, agent_id: str, hb: HeartbeatPayload) -> None:
        pass

    async def on_violation(self, agent_id: str, v: ViolationPayload) -> None:
        self.violations.append(v)
        self._changed.set()

    async def on_screen_frame(self, agent_id: str, f: ScreenFramePayload) -> None:
        pass

    async def on_ack(self, agent_id: str, ack: AckPayload) -> None:
        pass

    async def until(self, cond: Callable[[], bool], timeout: float = 15.0) -> None:
        async def wait() -> None:
            while not cond():
                self._changed.clear()
                await self._changed.wait()

        await asyncio.wait_for(wait(), timeout=timeout)

    def tamper(self) -> list[ViolationPayload]:
        return [v for v in self.violations if v.kind is ViolationKind.AGENT_TAMPER]


@pytest.fixture(autouse=True)
def fake_registry(monkeypatch: pytest.MonkeyPatch) -> FakeRegistry:
    reg = FakeRegistry()
    monkeypatch.setattr(app_mod, "_registry", lambda: reg)
    return reg


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    d = Database(tmp_path / "dash.sqlite")
    yield d
    d.close()


@pytest.fixture
async def dashboard(db: Database) -> AsyncIterator[tuple[str, PairingService, Events]]:
    pairing = PairingService(db)
    events = Events()
    srv = WebSocketServer(
        host="127.0.0.1", port=0, authenticator=pairing,
        ruleset_provider=lambda hello: RuleSet(revision=1, dry_run=True),
        events=events, heartbeat_interval_s=0.5,
    )
    await srv.start()
    yield f"ws://127.0.0.1:{srv.bound_port}", pairing, events
    await srv.stop()


def _paired_config(tmp_path: Path, url: str, pairing: PairingService) -> tuple[AgentConfig, Path]:
    data = tmp_path / "agent"
    config = AgentConfig(data_dir=data, token_file=data / "token", direct_url=url)
    agent_id = app_mod.stable_agent_id(config)
    token = pairing.pair(agent_id=agent_id, hostname="LAB-X", code=pairing.code,
                         source="10.9.9.9").token
    assert token is not None
    store_token(config, token)
    toml = tmp_path / "agent.toml"
    toml.write_text(
        f'data_dir = "{data.as_posix()}"\ntoken_file = "{(data / "token").as_posix()}"\n'
        f'direct_url = "{url}"\n', encoding="utf-8",
    )
    return config, toml


def _spawn(toml: Path) -> subprocess.Popen[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(str(ROOT / p) for p in ("shared", "agent", "dashboard"))
    return subprocess.Popen(
        [sys.executable, "-m", "labsentinel_agent", "--config", str(toml)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


async def _exit_code(proc: subprocess.Popen[str], timeout: float = 15.0) -> int:
    return await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=timeout)


@contextlib.asynccontextmanager
async def _in_process(config: AgentConfig) -> AsyncIterator[LabSentinelAgent]:
    agent = LabSentinelAgent(config)
    task = asyncio.ensure_future(agent.run())
    try:
        yield agent
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await agent.shutdown()


async def test_killed_agent_is_reported_after_restart(
    dashboard: tuple[str, PairingService, Events], tmp_path: Path
) -> None:
    url, pairing, events = dashboard
    config, toml = _paired_config(tmp_path, url, pairing)

    proc = _spawn(toml)
    try:
        await events.until(lambda: len(events.connected) == 1)
        proc.send_signal(signal.SIGKILL)  # what "End task" does: no cleanup at all
        await _exit_code(proc)
    finally:
        proc.kill()
    assert events.tamper() == []  # nothing can be said by a dead process

    async with _in_process(config):  # the service manager restarts it
        await events.until(lambda: len(events.tamper()) == 1)
    report = events.tamper()[0]
    assert "dihentikan paksa" in report.detail
    assert report.dry_run is False


async def test_normal_stop_is_not_reported_as_tamper(
    dashboard: tuple[str, PairingService, Events], tmp_path: Path
) -> None:
    url, pairing, events = dashboard
    config, toml = _paired_config(tmp_path, url, pairing)

    proc = _spawn(toml)
    try:
        await events.until(lambda: len(events.connected) == 1)
        proc.send_signal(signal.SIGTERM)
        assert await _exit_code(proc) == 0, proc.stdout.read() if proc.stdout else ""
    finally:
        proc.kill()
    assert not (config.data_dir / "running.json").exists()

    async with _in_process(config):
        await events.until(lambda: len(events.connected) == 2)
        await asyncio.sleep(0.5)
    assert events.tamper() == []


async def test_manual_service_stop_is_reported_on_next_start(
    dashboard: tuple[str, PairingService, Events], tmp_path: Path
) -> None:
    url, pairing, events = dashboard
    config, _ = _paired_config(tmp_path, url, pairing)

    runner = ServiceRunner(lambda: LabSentinelAgent(config))
    await asyncio.to_thread(runner.start)
    await events.until(lambda: len(events.connected) == 1)
    assert await asyncio.to_thread(runner.stop, reason="service_stopped", timeout=10)
    assert events.tamper() == []

    async with _in_process(config):
        await events.until(lambda: len(events.tamper()) == 1)
    assert "dihentikan secara manual" in events.tamper()[0].detail


async def test_second_instance_refuses_and_leaves_the_first_alone(
    dashboard: tuple[str, PairingService, Events], tmp_path: Path,
    fake_registry: FakeRegistry,
) -> None:
    url, pairing, events = dashboard
    config, toml = _paired_config(tmp_path, url, pairing)
    proc = _spawn(toml)
    try:
        await events.until(lambda: len(events.connected) == 1)
        second = LabSentinelAgent(config)
        assert await second.run() == EXIT_ALREADY_RUNNING
        await second.shutdown()  # must not stop or unmark the running agent
        assert (config.data_dir / "running.json").exists()
        await asyncio.sleep(1.5)
        assert proc.poll() is None
        assert events.connected == [events.connected[0]]  # no flapping
    finally:
        proc.send_signal(signal.SIGTERM)
        with contextlib.suppress(Exception):
            await _exit_code(proc)
        proc.kill()
