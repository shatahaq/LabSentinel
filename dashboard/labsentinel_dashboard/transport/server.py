"""Agent-facing WebSocket server.

Runs in its own asyncio loop (separate from Eel/gevent). Responsibilities:

* Accept agent connections and run the auth handshake (HELLO -> WELCOME/REJECT).
* Validate every inbound envelope (schema + token + replay), dropping bad ones.
* Keep one :class:`AgentSession` per connected agent and expose a thread-safe
  way for the rest of the dashboard to push commands / rules to agents.
* Surface agent events to an injected :class:`ServerEvents` sink (storage, UI).

Transport is deliberately decoupled from domain/storage via ``ServerEvents`` and
``RulesetProvider`` so it can be unit/integration tested with fakes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import websockets
from websockets.asyncio.server import Server, ServerConnection, serve

from labsentinel_shared.enums import MsgType
from labsentinel_shared.protocol import (
    PROTOCOL_VERSION,
    AckPayload,
    CommandPayload,
    Envelope,
    GoodbyePayload,
    HeartbeatPayload,
    HelloPayload,
    ProtocolError,
    RejectPayload,
    RulesUpdatePayload,
    ScreenFramePayload,
    ViolationPayload,
    WelcomePayload,
    decode,
    encode,
)
from labsentinel_shared.rules import RuleSet
from labsentinel_shared.security import ReplayGuard

log = logging.getLogger("labsentinel.dashboard.server")

RulesetProvider = Callable[[HelloPayload], RuleSet]


@dataclass(frozen=True)
class AuthResult:
    """Outcome of authenticating a HELLO."""

    ok: bool
    issued_token: str | None = None  # set when this HELLO completed pairing
    reason: str = ""


class Authenticator(Protocol):
    """Decides whether an agent may join, and with which credential.

    Separated from the transport so pairing policy (short code, rate limits,
    per-agent tokens) lives in the domain and can be tested on its own.
    """

    def authenticate_hello(
        self, hello: HelloPayload, token: str | None, *, source: str
    ) -> AuthResult:
        """Authenticate a first message: existing token, or a pairing code."""
        ...

    def authenticate_message(self, agent_id: str, token: str | None) -> bool:
        """Authenticate every subsequent message from an established session."""
        ...


class ServerEvents(Protocol):
    """Sink for agent events. Implemented by the dashboard's domain/storage."""

    async def on_agent_connected(self, session: AgentSession) -> None: ...
    async def on_agent_disconnected(self, agent_id: str, *, clean: bool) -> None: ...
    async def on_heartbeat(self, agent_id: str, hb: HeartbeatPayload) -> None: ...
    async def on_violation(self, agent_id: str, v: ViolationPayload) -> None: ...
    async def on_screen_frame(self, agent_id: str, f: ScreenFramePayload) -> None: ...
    async def on_ack(self, agent_id: str, ack: AckPayload) -> None: ...


@dataclass
class AgentSession:
    """Server-side handle for one connected agent."""

    agent_id: str
    hostname: str
    username: str
    agent_version: str
    connection: ServerConnection
    session_id: str
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    remote_addr: str = ""
    clean_goodbye: bool = False

    async def send(self, payload: object, *, token: str | None = None) -> None:
        """Serialize + send a payload to this agent. Raises on dead socket."""
        await self.connection.send(encode(payload, token=token))  # type: ignore[arg-type]

    async def send_ruleset(self, ruleset: RuleSet) -> None:
        await self.send(RulesUpdatePayload(ruleset=ruleset))

    async def send_command(self, cmd: CommandPayload) -> str:
        """Send a command and return the envelope id for ACK correlation."""
        env = Envelope(payload=cmd)
        await self.connection.send(env.to_json())
        return env.id


class WebSocketServer:
    """Manages all agent connections for the dashboard."""

    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 8765,
        authenticator: Authenticator,
        ruleset_provider: RulesetProvider,
        events: ServerEvents,
        reply_token: str | None = None,
        heartbeat_interval_s: float = 5.0,
        max_message_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        self._host = host
        self._port = port
        self._auth = authenticator
        self._ruleset_for = ruleset_provider
        self._events = events
        self._reply_token = reply_token
        self._hb_interval = heartbeat_interval_s
        self._max_bytes = max_message_bytes
        self._sessions: dict[str, AgentSession] = {}
        self._replay = ReplayGuard()
        self._server: Server | None = None
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self._server = await serve(
            self._handle,
            self._host,
            self._port,
            max_size=self._max_bytes,
            ping_interval=20,
            ping_timeout=20,
        )
        log.info("WebSocket server listening on ws://%s:%d", self._host, self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def bound_port(self) -> int:
        """Actual port (useful when started with port 0 in tests)."""
        if self._server and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self._port

    # -- outbound (called from the dashboard side) -------------------------
    def sessions(self) -> list[AgentSession]:
        return list(self._sessions.values())

    def get(self, agent_id: str) -> AgentSession | None:
        return self._sessions.get(agent_id)

    async def broadcast_command(self, cmd: CommandPayload) -> dict[str, str]:
        """Send a command to every connected agent. Returns agent_id -> ack_id."""
        result: dict[str, str] = {}
        for session in list(self._sessions.values()):
            with contextlib.suppress(Exception):
                result[session.agent_id] = await session.send_command(cmd)
        return result

    # -- connection handling ----------------------------------------------
    async def _handle(self, conn: ServerConnection) -> None:
        remote = _peer(conn)
        session: AgentSession | None = None
        try:
            session = await self._handshake(conn, remote)
            if session is None:
                return
            async for raw in conn:
                await self._dispatch(session, raw)
        except websockets.ConnectionClosed:
            pass
        except Exception:  # never let one bad connection take down the server
            log.exception("unexpected error on connection from %s", remote)
        finally:
            if session is not None:
                await self._drop(session, clean=session.clean_goodbye)

    async def _handshake(self, conn: ServerConnection, remote: str) -> AgentSession | None:
        try:
            raw = await asyncio.wait_for(conn.recv(), timeout=10)
        except (TimeoutError, websockets.ConnectionClosed):
            return None

        try:
            env = decode(raw, max_bytes=self._max_bytes)
        except ProtocolError as exc:
            await self._reject(conn, f"malformed hello: {exc}")
            return None

        if not env.version_compatible():
            await self._reject(conn, "protocol version mismatch")
            return None
        if not isinstance(env.payload, HelloPayload):
            await self._reject(conn, "first message must be hello")
            return None
        if not self._replay.check(env.id, env.ts):
            await self._reject(conn, "replayed or stale hello")
            return None

        hello = env.payload
        # The envelope token is the agent's own credential once paired; the
        # pairing code inside the payload is only for first contact.
        auth = self._auth.authenticate_hello(hello, env.token, source=_host_of(remote))
        if not auth.ok:
            log.warning("rejected agent from %s: %s", remote, auth.reason or "auth failed")
            await self._reject(conn, auth.reason or "authentication failed")
            return None
        session = AgentSession(
            agent_id=hello.agent_id,
            hostname=hello.hostname,
            username=hello.username,
            agent_version=hello.agent_version,
            connection=conn,
            session_id=_new_session_id(),
            remote_addr=remote,
        )
        async with self._lock:
            # Replace any stale session for the same agent id.
            existing = self._sessions.get(hello.agent_id)
            if existing is not None:
                with contextlib.suppress(Exception):
                    await existing.connection.close()
            self._sessions[hello.agent_id] = session

        ruleset = self._ruleset_for(hello)
        await session.send(
            WelcomePayload(
                session_id=session.session_id,
                server_time=time.time(),
                heartbeat_interval_s=self._hb_interval,
                ruleset=ruleset,
                # Present only when this HELLO completed pairing.
                agent_token=auth.issued_token,
            ),
            token=self._reply_token,
        )
        log.info("agent %s (%s) connected from %s", hello.agent_id, hello.hostname, remote)
        await self._events.on_agent_connected(session)
        return session

    async def _dispatch(self, session: AgentSession, raw: str | bytes) -> None:
        try:
            env = decode(raw, max_bytes=self._max_bytes)
        except ProtocolError as exc:
            log.warning("dropping bad message from %s: %s", session.agent_id, exc)
            return
        if not self._auth.authenticate_message(session.agent_id, env.token):
            log.warning("dropping unauthenticated message from %s", session.agent_id)
            return
        if not self._replay.check(env.id, env.ts):
            return

        session.last_seen = time.time()
        payload = env.payload
        kind = payload.type

        if kind == MsgType.HEARTBEAT and isinstance(payload, HeartbeatPayload):
            await self._events.on_heartbeat(session.agent_id, payload)
        elif kind == MsgType.VIOLATION and isinstance(payload, ViolationPayload):
            await self._events.on_violation(session.agent_id, payload)
        elif kind == MsgType.SCREEN_FRAME and isinstance(payload, ScreenFramePayload):
            await self._events.on_screen_frame(session.agent_id, payload)
        elif kind == MsgType.ACK and isinstance(payload, AckPayload):
            await self._events.on_ack(session.agent_id, payload)
        elif kind == MsgType.GOODBYE and isinstance(payload, GoodbyePayload):
            session.clean_goodbye = True
            with contextlib.suppress(Exception):
                await session.connection.close()
        else:
            log.debug("ignoring unexpected %s from %s", kind, session.agent_id)

    async def _drop(self, session: AgentSession, *, clean: bool) -> None:
        async with self._lock:
            if self._sessions.get(session.agent_id) is session:
                del self._sessions[session.agent_id]
        await self._events.on_agent_disconnected(session.agent_id, clean=clean)
        log.info("agent %s disconnected (clean=%s)", session.agent_id, clean)

    async def _reject(self, conn: ServerConnection, reason: str) -> None:
        with contextlib.suppress(Exception):
            await conn.send(
                encode(
                    RejectPayload(reason=reason, server_protocol_version=PROTOCOL_VERSION),
                    token=self._reply_token,
                )
            )
            await conn.close()


def _host_of(remote: str) -> str:
    """Strip the port, so rate limiting keys on the machine, not the socket."""
    return remote.rsplit(":", 1)[0] if ":" in remote else remote


def _peer(conn: ServerConnection) -> str:
    try:
        addr = conn.remote_address
        return f"{addr[0]}:{addr[1]}" if addr else "?"
    except Exception:
        return "?"


def _new_session_id() -> str:
    import uuid

    return uuid.uuid4().hex[:16]
