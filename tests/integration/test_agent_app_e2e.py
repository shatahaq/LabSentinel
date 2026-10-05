"""The whole agent program against a real dashboard server.

Unlike test_pairing_flow.py (which drives DashboardClient directly), this runs
LabSentinelAgent itself: recovery, tray bridge, detection, streaming and the
link, exactly as ``labsentinel-agent --pair KODE`` wires them. Off Windows the
enforcement layer protects every process (no console session), so this is safe
to run on any machine.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

import labsentinel_agent.app as app_mod
from labsentinel_agent.app import EXIT_PAIRING_REJECTED, LabSentinelAgent, read_token
from labsentinel_agent.policy.registry import FakeRegistry
from labsentinel_dashboard.domain.pairing import PairingService
from labsentinel_dashboard.storage.db import Database
from labsentinel_dashboard.transport.server import AgentSession, WebSocketServer
from labsentinel_shared.config import AgentConfig
from labsentinel_shared.protocol import (
    AckPayload,
    HeartbeatPayload,
    ScreenFramePayload,
    ViolationPayload,
)
from labsentinel_shared.rules import RuleSet

pytestmark = pytest.mark.integration


class Events:
    def __init__(self) -> None:
        self.connected: list[AgentSession] = []
        self.heartbeats = 0
        self._changed = asyncio.Event()

    async def on_agent_connected(self, session: AgentSession) -> None:
        self.connected.append(session)
        self._changed.set()

    async def on_agent_disconnected(self, agent_id: str, *, clean: bool) -> None:
        self._changed.set()

    async def on_heartbeat(self, agent_id: str, hb: HeartbeatPayload) -> None:
        self.heartbeats += 1
        self._changed.set()

    async def on_violation(self, agent_id: str, v: ViolationPayload) -> None:
        pass

    async def on_screen_frame(self, agent_id: str, f: ScreenFramePayload) -> None:
        pass

    async def on_ack(self, agent_id: str, ack: AckPayload) -> None:
        pass

    async def until(self, cond: object, timeout: float = 10.0) -> None:
        async def wait() -> None:
            while not cond():  # type: ignore[operator]
                self._changed.clear()
                await self._changed.wait()

        await asyncio.wait_for(wait(), timeout=timeout)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    d = Database(tmp_path / "dash.sqlite")
    yield d
    d.close()


@pytest.fixture(autouse=True)
def fake_registry(monkeypatch: pytest.MonkeyPatch) -> FakeRegistry:
    reg = FakeRegistry()
    monkeypatch.setattr(app_mod, "_registry", lambda: reg)
    return reg


@pytest.fixture
async def dashboard(db: Database) -> AsyncIterator[tuple[str, PairingService, Events]]:
    pairing = PairingService(db)
    events = Events()
    srv = WebSocketServer(
        host="127.0.0.1", port=0, authenticator=pairing,
        ruleset_provider=lambda hello: RuleSet(revision=5, dry_run=True),
        events=events, heartbeat_interval_s=0.3,
    )
    await srv.start()
    yield f"ws://127.0.0.1:{srv.bound_port}", pairing, events
    await srv.stop()


def _config(tmp_path: Path, url: str) -> AgentConfig:
    data = tmp_path / "agent"
    return AgentConfig(data_dir=data, token_file=data / "token", direct_url=url)


@contextlib.asynccontextmanager
async def _agent(config: AgentConfig, code: str | None) -> AsyncIterator[LabSentinelAgent]:
    agent = LabSentinelAgent(config, pairing_code=code)
    task = asyncio.ensure_future(agent.run())
    try:
        yield agent
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await agent.shutdown()


async def test_agent_pairs_then_restarts_with_its_stored_token(
    dashboard: tuple[str, PairingService, Events], tmp_path: Path, db: Database
) -> None:
    url, pairing, events = dashboard
    config = _config(tmp_path, url)

    # First install: labsentinel-agent --pair KODE
    async with _agent(config, pairing.code) as agent:
        await events.until(lambda: len(events.connected) == 1 and events.heartbeats >= 1)
        assert events.connected[0].agent_id == agent.agent_id
        first_id = agent.agent_id
    token = read_token(config)
    assert token is not None
    assert db.get_agent_token_hash(first_id) is not None

    # Admin closes registration once the lab is installed.
    pairing.set_open(False, actor="pengawas")

    # Every later start (service, reboot): no code, same identity, same token.
    beats = events.heartbeats
    async with _agent(config, None) as agent:
        await events.until(lambda: len(events.connected) == 2 and events.heartbeats > beats)
        assert agent.agent_id == first_id
        assert agent.cache.current is not None
        assert agent.cache.current.revision == 5  # rules applied and cached
    assert read_token(config) == token  # nothing was re-issued


async def test_agent_with_wrong_code_stops_and_reports(
    dashboard: tuple[str, PairingService, Events], tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url, pairing, events = dashboard
    wrong = "100000" if pairing.code != "100000" else "100001"
    agent = LabSentinelAgent(_config(tmp_path, url), pairing_code=wrong)
    try:
        code = await asyncio.wait_for(agent.run(), timeout=10)
    finally:
        await agent.shutdown()
    assert code == EXIT_PAIRING_REJECTED
    assert events.connected == []
    assert "kode pemasangan salah" in capsys.readouterr().out


async def test_repair_after_dashboard_reset(
    dashboard: tuple[str, PairingService, Events], tmp_path: Path, db: Database
) -> None:
    """The dashboard lost its database: the old token is unknown. Running once
    with --pair must fix it without deleting files by hand."""
    url, pairing, events = dashboard
    config = _config(tmp_path, url)
    config.token_file.parent.mkdir(parents=True)
    config.token_file.write_text("token-from-before-the-reset", encoding="utf-8")

    async with _agent(config, pairing.code):
        await events.until(lambda: len(events.connected) == 1)
    assert read_token(config) not in (None, "token-from-before-the-reset")


# ------------------------------------------------------------- --pair-only
async def test_pair_only_pairs_and_exits_without_enforcing(
    dashboard: tuple[str, PairingService, Events], tmp_path: Path, db: Database,
    capsys: pytest.CaptureFixture[str],
) -> None:
    url, pairing, _events = dashboard
    config = _config(tmp_path, url)
    agent = LabSentinelAgent(config, pairing_code=pairing.code, pair_only=True)
    try:
        code = await asyncio.wait_for(agent.run(), timeout=15)
    finally:
        await agent.shutdown()
    assert code == app_mod.EXIT_OK
    assert read_token(config) is not None
    assert db.get_agent_token_hash(agent.agent_id) is not None
    # Installer step only: no tray endpoint, no run marker, no cached rules.
    for leftover in ("tray_endpoint", "running.json", "ruleset.json"):
        assert not (config.data_dir / leftover).exists(), leftover
    assert "berhasil dipasangkan" in capsys.readouterr().out


async def test_pair_only_with_wrong_code(
    dashboard: tuple[str, PairingService, Events], tmp_path: Path
) -> None:
    url, pairing, _events = dashboard
    wrong = "100000" if pairing.code != "100000" else "100001"
    agent = LabSentinelAgent(_config(tmp_path, url), pairing_code=wrong, pair_only=True)
    try:
        assert await asyncio.wait_for(agent.run(), timeout=15) == EXIT_PAIRING_REJECTED
    finally:
        await agent.shutdown()


async def test_pair_only_without_a_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import socket

    with socket.socket() as s:  # a port nobody listens on
        s.bind(("127.0.0.1", 0))
        dead = f"ws://127.0.0.1:{s.getsockname()[1]}"
    monkeypatch.setattr(app_mod, "PAIR_ONLY_TIMEOUT_S", 1.0)
    agent = LabSentinelAgent(_config(tmp_path, dead), pairing_code="123456", pair_only=True)
    try:
        assert await asyncio.wait_for(agent.run(), timeout=15) == app_mod.EXIT_NO_DASHBOARD
    finally:
        await agent.shutdown()
    assert "Dashboard tidak ditemukan" in capsys.readouterr().out
