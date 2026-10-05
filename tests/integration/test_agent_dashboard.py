"""End-to-end M1 transport tests over real localhost WebSockets.

Proves: discovery-independent connect, token auth + rejection, WELCOME ruleset
delivery, heartbeat flow, live rule updates, command -> ACK correlation,
agent-originated violations, and clean disconnect classification.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import pytest
import websockets

from auth_helpers import SharedTokenAuth
from labsentinel_agent.transport.client import (
    AgentIdentity,
    BackoffPolicy,
    CommandResult,
    DashboardClient,
)
from labsentinel_dashboard.transport.server import AgentSession, WebSocketServer
from labsentinel_shared.enums import AgentState, CommandType, ViolationKind
from labsentinel_shared.protocol import (
    AckPayload,
    CommandPayload,
    HeartbeatPayload,
    HelloPayload,
    ScreenFramePayload,
    ViolationPayload,
    decode,
    encode,
)
from labsentinel_shared.rules import RuleSet

pytestmark = pytest.mark.integration

TOKEN = "integration-test-token"


class CollectingEvents:
    """Fake ServerEvents that records everything with wait-able signals."""

    def __init__(self) -> None:
        self.connected: list[str] = []
        self.disconnected: list[tuple[str, bool]] = []
        self.heartbeats: list[HeartbeatPayload] = []
        self.violations: list[ViolationPayload] = []
        self.frames: list[ScreenFramePayload] = []
        self.acks: list[AckPayload] = []
        self._hb_evt = asyncio.Event()
        self._viol_evt = asyncio.Event()
        self._ack_evt = asyncio.Event()
        self._conn_evt = asyncio.Event()
        self._disc_evt = asyncio.Event()

    async def on_agent_connected(self, session: AgentSession) -> None:
        self.connected.append(session.agent_id)
        self._conn_evt.set()

    async def on_agent_disconnected(self, agent_id: str, *, clean: bool) -> None:
        self.disconnected.append((agent_id, clean))
        self._disc_evt.set()

    async def on_heartbeat(self, agent_id: str, hb: HeartbeatPayload) -> None:
        self.heartbeats.append(hb)
        self._hb_evt.set()

    async def on_violation(self, agent_id: str, v: ViolationPayload) -> None:
        self.violations.append(v)
        self._viol_evt.set()

    async def on_screen_frame(self, agent_id: str, f: ScreenFramePayload) -> None:
        self.frames.append(f)

    async def on_ack(self, agent_id: str, ack: AckPayload) -> None:
        self.acks.append(ack)
        self._ack_evt.set()

    async def wait_hb(self) -> None:
        await asyncio.wait_for(self._hb_evt.wait(), timeout=5)

    async def wait_violation(self) -> None:
        await asyncio.wait_for(self._viol_evt.wait(), timeout=5)

    async def wait_ack(self) -> None:
        await asyncio.wait_for(self._ack_evt.wait(), timeout=5)

    async def wait_connected(self) -> None:
        await asyncio.wait_for(self._conn_evt.wait(), timeout=5)

    async def wait_disconnected(self) -> None:
        await asyncio.wait_for(self._disc_evt.wait(), timeout=5)


async def _start_server(events: CollectingEvents, ruleset: RuleSet) -> WebSocketServer:
    server = WebSocketServer(
        host="127.0.0.1",
        port=0,
        authenticator=SharedTokenAuth(TOKEN),
        ruleset_provider=lambda hello: ruleset,
        events=events,
        reply_token=TOKEN,
    )
    await server.start()
    return server


def _make_client(
    url: str, *, on_ruleset, on_command, hb_interval: float = 0.2
) -> DashboardClient:
    return DashboardClient(
        identity=AgentIdentity(agent_id="pc-01", hostname="LAB-PC-01", username="student"),
        token=TOKEN,
        state_provider=lambda: AgentState.ENFORCING,
        metrics_provider=lambda: (1.5, 42.0, "Chrome"),
        on_ruleset=on_ruleset,
        on_command=on_command,
        cached_revision_provider=lambda: 3,
        heartbeat_interval_s=hb_interval,
        backoff=BackoffPolicy(base_s=0.1, max_s=0.3, jitter_s=0.0),
    )


@pytest.mark.asyncio
async def test_full_m1_flow() -> None:
    events = CollectingEvents()
    ruleset = RuleSet(revision=10, dry_run=True)
    server = await _start_server(events, ruleset)
    url = f"ws://127.0.0.1:{server.bound_port}"

    received_rulesets: list[RuleSet] = []
    received_commands: list[CommandPayload] = []

    async def on_ruleset(rs: RuleSet) -> None:
        received_rulesets.append(rs)

    async def on_command(cmd: CommandPayload) -> CommandResult:
        received_commands.append(cmd)
        new_state = AgentState.PAUSED if cmd.command == CommandType.PAUSE else AgentState.ENFORCING
        return CommandResult(resulting_state=new_state)

    client = _make_client(url, on_ruleset=on_ruleset, on_command=on_command)
    run_task = asyncio.ensure_future(client.run(lambda: _ret(url)))

    try:
        # --- handshake + WELCOME ruleset ---
        await asyncio.wait_for(client.connected.wait(), timeout=5)
        await events.wait_connected()
        assert events.connected == ["pc-01"]
        assert received_rulesets and received_rulesets[0].revision == 10

        session = server.get("pc-01")
        assert session is not None and session.hostname == "LAB-PC-01"

        # --- heartbeat flows agent -> dashboard ---
        await events.wait_hb()
        assert events.heartbeats[0].state is AgentState.ENFORCING
        assert events.heartbeats[0].cpu_percent == pytest.approx(1.5)

        # --- live rule update dashboard -> agent ---
        await session.send_ruleset(RuleSet(revision=11, dry_run=False))
        await _until(lambda: any(r.revision == 11 for r in received_rulesets))
        assert received_rulesets[-1].dry_run is False

        # --- command -> ACK correlation ---
        ack_id = await session.send_command(
            CommandPayload(command=CommandType.PAUSE, issued_by="dosen@lab")
        )
        await events.wait_ack()
        assert events.acks[0].ack_id == ack_id
        assert events.acks[0].resulting_state is AgentState.PAUSED
        assert received_commands[0].command is CommandType.PAUSE

        # --- agent-originated violation ---
        sent = await client.send_event(
            ViolationPayload(
                agent_id="pc-01",
                kind=ViolationKind.APP_BLOCKED,
                subject="game.exe",
                occurred_at=time.time(),
                dry_run=True,
            )
        )
        assert sent is True
        await events.wait_violation()
        assert events.violations[0].subject == "game.exe"
        assert events.violations[0].dry_run is True

        # --- clean disconnect via goodbye ---
        await client.stop()
        await events.wait_disconnected()
        assert events.disconnected[0] == ("pc-01", True)
    finally:
        await client.stop()
        # stop() makes run() exit cleanly; it should finish without cancellation.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(run_task, timeout=5)
        if not run_task.done():
            run_task.cancel()
        await server.stop()


@pytest.mark.asyncio
async def test_bad_token_is_rejected() -> None:
    events = CollectingEvents()
    server = await _start_server(events, RuleSet(revision=1))
    url = f"ws://127.0.0.1:{server.bound_port}"
    try:
        async with websockets.connect(url) as conn:
            await conn.send(
                encode(HelloPayload(agent_id="evil", hostname="x"), token="WRONG-TOKEN")
            )
            raw = await asyncio.wait_for(conn.recv(), timeout=5)
            env = decode(raw)
            assert env.payload.type == "reject"
            assert "authentication" in env.payload.reason.lower()  # type: ignore[union-attr]
    finally:
        await server.stop()
    assert events.connected == []


@pytest.mark.asyncio
async def test_non_hello_first_message_rejected() -> None:
    events = CollectingEvents()
    server = await _start_server(events, RuleSet(revision=1))
    url = f"ws://127.0.0.1:{server.bound_port}"
    try:
        async with websockets.connect(url) as conn:
            await conn.send(
                encode(
                    HeartbeatPayload(agent_id="x", state=AgentState.ENFORCING, rule_revision=0),
                    token=TOKEN,
                )
            )
            env = decode(await asyncio.wait_for(conn.recv(), timeout=5))
            assert env.payload.type == "reject"
    finally:
        await server.stop()


async def _ret(url: str) -> str:
    return url


async def _until(pred, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")
