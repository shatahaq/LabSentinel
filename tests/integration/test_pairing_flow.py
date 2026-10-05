"""Pairing end to end: a real agent client against a real dashboard server.

The agent types the 6-digit code once, receives its own token over the
WebSocket, stores it, and every later connection uses only that token. Nothing
here is faked except the event sink: authentication is the real
:class:`PairingService` on a real SQLite database.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from labsentinel_agent.transport.client import (
    AgentIdentity,
    BackoffPolicy,
    CommandResult,
    DashboardClient,
)
from labsentinel_dashboard.domain.pairing import PairingService
from labsentinel_dashboard.storage.db import Database
from labsentinel_dashboard.transport.server import AgentSession, WebSocketServer
from labsentinel_shared.enums import AgentState
from labsentinel_shared.protocol import (
    AckPayload,
    CommandPayload,
    HeartbeatPayload,
    ScreenFramePayload,
    ViolationPayload,
)
from labsentinel_shared.rules import RuleSet
from labsentinel_shared.security import hash_token

pytestmark = pytest.mark.integration


class Events:
    def __init__(self) -> None:
        self.connected: list[str] = []
        self.heartbeats: list[str] = []
        self._hb = asyncio.Event()

    async def on_agent_connected(self, session: AgentSession) -> None:
        self.connected.append(session.agent_id)

    async def on_agent_disconnected(self, agent_id: str, *, clean: bool) -> None:
        pass

    async def on_heartbeat(self, agent_id: str, hb: HeartbeatPayload) -> None:
        self.heartbeats.append(agent_id)
        self._hb.set()

    async def on_violation(self, agent_id: str, v: ViolationPayload) -> None:
        pass

    async def on_screen_frame(self, agent_id: str, f: ScreenFramePayload) -> None:
        pass

    async def on_ack(self, agent_id: str, ack: AckPayload) -> None:
        pass

    async def wait_heartbeat(self) -> None:
        self._hb.clear()
        await asyncio.wait_for(self._hb.wait(), timeout=5)


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    d = Database(tmp_path / "dash.sqlite")
    yield d
    d.close()


@pytest.fixture
def pairing(db: Database) -> PairingService:
    return PairingService(db)


@pytest.fixture
async def server(pairing: PairingService) -> AsyncIterator[tuple[WebSocketServer, Events]]:
    events = Events()
    srv = WebSocketServer(
        host="127.0.0.1",
        port=0,
        authenticator=pairing,
        ruleset_provider=lambda hello: RuleSet(revision=3),
        events=events,
        heartbeat_interval_s=0.2,
    )
    await srv.start()
    yield srv, events
    await srv.stop()


def _client(
    *,
    token: str | None,
    code: str | None,
    issued: list[str],
    rulesets: list[int],
    agent_id: str = "lab-pc-07",
) -> DashboardClient:
    async def on_ruleset(rs: RuleSet) -> None:
        rulesets.append(rs.revision)

    async def on_command(cmd: CommandPayload) -> CommandResult:
        return CommandResult(resulting_state=AgentState.ENFORCING)

    return DashboardClient(
        identity=AgentIdentity(agent_id=agent_id, hostname=agent_id.upper()),
        token=token,
        pairing_code=code,
        on_token_issued=issued.append,
        state_provider=lambda: AgentState.ENFORCING,
        metrics_provider=lambda: (0.0, 0.0, ""),
        on_ruleset=on_ruleset,
        on_command=on_command,
        heartbeat_interval_s=0.2,
        backoff=BackoffPolicy(base_s=0.05, max_s=0.1, jitter_s=0.0),
    )


@contextlib.asynccontextmanager
async def _running(client: DashboardClient, url: str) -> AsyncIterator[asyncio.Task[None]]:
    async def provide() -> str:
        return url

    task = asyncio.ensure_future(client.run(provide))
    try:
        yield task
    finally:
        await client.stop()
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(task, timeout=5)


async def test_pair_with_code_then_reconnect_with_stored_token(
    server: tuple[WebSocketServer, Events], pairing: PairingService, db: Database, tmp_path: Path
) -> None:
    srv, events = server
    url = f"ws://127.0.0.1:{srv.bound_port}"
    token_file = tmp_path / "agent" / "token"

    # --- first install: the admin types the code shown on the dashboard
    issued: list[str] = []
    rulesets: list[int] = []
    first = _client(token=None, code=pairing.code, issued=issued, rulesets=rulesets)
    async with _running(first, url):
        await asyncio.wait_for(first.connected.wait(), timeout=5)
        await events.wait_heartbeat()  # later messages authenticate with the new token

    assert len(issued) == 1
    token = issued[0]
    assert db.get_agent_token_hash("lab-pc-07") == hash_token(token)
    assert rulesets[0] == 3
    token_file.parent.mkdir(parents=True)
    token_file.write_text(token, encoding="utf-8")  # what the agent app does

    # The code is single-purpose: closing pairing and rotating it must not
    # affect a PC that is already paired.
    pairing.set_open(False, actor="pengawas")
    pairing.rotate_code(actor="pengawas")

    # --- every later start: only the stored token, no code at all
    issued_again: list[str] = []
    second = _client(
        token=token_file.read_text(encoding="utf-8"), code=None,
        issued=issued_again, rulesets=[],
    )
    async with _running(second, url):
        await asyncio.wait_for(second.connected.wait(), timeout=5)
        await events.wait_heartbeat()

    assert issued_again == []  # no new credential was handed out
    assert db.get_agent_token_hash("lab-pc-07") == hash_token(token)
    assert events.connected == ["lab-pc-07", "lab-pc-07"]
    assert [r.action for r in db.list_audit()].count("pairing.success") == 1


async def test_wrong_code_never_connects_and_stops_retrying(
    server: tuple[WebSocketServer, Events], pairing: PairingService, db: Database
) -> None:
    srv, events = server
    url = f"ws://127.0.0.1:{srv.bound_port}"
    wrong = "100000" if pairing.code != "100000" else "100001"
    issued: list[str] = []
    client = _client(token=None, code=wrong, issued=issued, rulesets=[])
    async with _running(client, url) as task:
        # The client gives up on its own instead of retrying the same bad code.
        await asyncio.wait_for(asyncio.shield(task), timeout=5)
        assert not client.connected.is_set()

    assert client.pairing_rejected is not None
    assert "salah" in client.pairing_rejected
    assert issued == []
    assert events.connected == []
    assert db.get_agent_token_hash("lab-pc-07") is None
    # One wrong attempt, not a burst of retries: the PC has not locked itself
    # out, so the admin can immediately try again with the right code.
    assert [r.action for r in db.list_audit()].count("pairing.failed") == 1
    result = pairing.pair(
        agent_id="lab-pc-07", hostname="LAB-PC-07", code=pairing.code, source="127.0.0.1"
    )
    assert result.ok


async def test_unknown_token_keeps_retrying(
    server: tuple[WebSocketServer, Events], pairing: PairingService
) -> None:
    """A revoked token is not a typo: keep trying so a re-pair or a restored
    dashboard database brings the PC back without anyone touching it."""
    srv, events = server
    client = _client(token="revoked-token", code=None, issued=[], rulesets=[])
    async with _running(client, f"ws://127.0.0.1:{srv.bound_port}") as task:
        await asyncio.sleep(0.4)
        assert not task.done()
        assert client.pairing_rejected is None
    assert events.connected == []


async def test_closed_pairing_rejects_new_pc_over_the_wire(
    server: tuple[WebSocketServer, Events], pairing: PairingService, db: Database
) -> None:
    srv, events = server
    pairing.set_open(False, actor="pengawas")
    client = _client(token=None, code=pairing.code, issued=[], rulesets=[])
    async with _running(client, f"ws://127.0.0.1:{srv.bound_port}"):
        await asyncio.sleep(0.4)
        assert not client.connected.is_set()
    assert events.connected == []
    assert db.get_agent_token_hash("lab-pc-07") is None


async def test_token_of_another_pc_is_refused(
    server: tuple[WebSocketServer, Events], pairing: PairingService
) -> None:
    srv, events = server
    stolen = pairing.pair(
        agent_id="lab-pc-01", hostname="LAB-PC-01", code=pairing.code, source="10.0.0.1"
    ).token
    client = _client(token=stolen, code=None, issued=[], rulesets=[], agent_id="lab-pc-02")
    async with _running(client, f"ws://127.0.0.1:{srv.bound_port}"):
        await asyncio.sleep(0.4)
        assert not client.connected.is_set()
    assert events.connected == []
