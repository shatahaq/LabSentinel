"""Dashboard-facing WebSocket client with auto-reconnect.

Connects to the dashboard (endpoint found via UDP discovery or configured
directly), authenticates with the pairing token, then runs two concurrent
loops: a heartbeat sender and a receiver that applies rule updates and executes
commands (acknowledging each one).

If the connection drops, it reconnects with exponential backoff + jitter. While
disconnected, enforcement elsewhere keeps running on the cached ruleset — this
client only concerns itself with the link.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import websockets

from labsentinel_shared.enums import AgentState, MsgType
from labsentinel_shared.protocol import (
    AckPayload,
    CommandPayload,
    GoodbyePayload,
    HeartbeatPayload,
    HelloPayload,
    ProtocolError,
    RejectPayload,
    RulesUpdatePayload,
    WelcomePayload,
    decode,
    encode,
)
from labsentinel_shared.rules import RuleSet

log = logging.getLogger("labsentinel.agent.client")


@dataclass(frozen=True)
class AgentIdentity:
    agent_id: str
    hostname: str
    username: str = ""
    agent_version: str = "0.1.0"
    os_version: str = ""


@dataclass(frozen=True)
class CommandResult:
    """What a command handler reports back; the client turns it into an ACK."""

    resulting_state: AgentState
    ok: bool = True
    message: str = ""


# Callbacks injected by the agent core (keeps transport free of enforcement).
StateProvider = Callable[[], AgentState]
MetricsProvider = Callable[[], tuple[float, float, str]]  # cpu%, mem_mb, active_title
RulesetHandler = Callable[[RuleSet], Awaitable[None]]
CommandHandler = Callable[[CommandPayload], Awaitable[CommandResult]]


@dataclass
class BackoffPolicy:
    base_s: float = 1.0
    factor: float = 2.0
    max_s: float = 30.0
    jitter_s: float = 0.5

    def delay(self, attempt: int) -> float:
        raw = min(self.max_s, self.base_s * (self.factor ** max(0, attempt - 1)))
        return raw + random.uniform(0, self.jitter_s)


class DashboardClient:
    def __init__(
        self,
        *,
        identity: AgentIdentity,
        token: str | None,
        pairing_code: str | None = None,
        on_token_issued: Callable[[str], None] | None = None,
        state_provider: StateProvider,
        metrics_provider: MetricsProvider,
        on_ruleset: RulesetHandler,
        on_command: CommandHandler,
        cached_revision_provider: Callable[[], int] = lambda: -1,
        indicator_visible_provider: Callable[[], bool] = lambda: True,
        desktop_access_provider: Callable[[], bool] = lambda: True,
        username_provider: Callable[[], str | None] = lambda: None,
        heartbeat_interval_s: float = 5.0,
        backoff: BackoffPolicy | None = None,
    ) -> None:
        self._id = identity
        self._token = token
        self._pairing_code = pairing_code
        self._on_token_issued = on_token_issued
        self._state = state_provider
        self._metrics = metrics_provider
        self._on_ruleset = on_ruleset
        self._on_command = on_command
        self._cached_rev = cached_revision_provider
        self._indicator_visible = indicator_visible_provider
        self._desktop_access = desktop_access_provider
        self._username = username_provider
        self._hb_interval = heartbeat_interval_s
        self._backoff = backoff or BackoffPolicy()
        self._stop = asyncio.Event()
        self._conn: websockets.ClientConnection | None = None
        self.connected = asyncio.Event()
        # Set when the dashboard refused our pairing code; run() then returns.
        self.pairing_rejected: str | None = None

    @property
    def agent_id(self) -> str:
        return self._id.agent_id

    async def run(self, url_provider: Callable[[], Awaitable[str | None]]) -> None:
        """Connect/reconnect forever until :meth:`stop` is called.

        ``url_provider`` returns a ws url (e.g. via discovery) or None to retry.
        """
        attempt = 0
        while not self._stop.is_set():
            url = await url_provider()
            if not url:
                await self._sleep(self._backoff.delay(attempt + 1))
                attempt += 1
                continue
            try:
                await self._session(url)
                attempt = 0  # a clean session resets backoff
            except (OSError, websockets.WebSocketException, ProtocolError) as exc:
                log.info("connection to %s failed/closed: %s", url, exc)
            except Exception:
                log.exception("unexpected client error")
            finally:
                self.connected.clear()
            if self._stop.is_set():
                break
            attempt += 1
            delay = self._backoff.delay(attempt)
            log.info("reconnecting in %.1fs (attempt %d)", delay, attempt)
            await self._sleep(delay)

    async def _session(self, url: str) -> None:
        # proxy=None: the dashboard is always on the lab LAN. websockets >= 15
        # otherwise follows the system proxy (env vars, and on Windows the
        # Internet Options setting many schools configure), which would route
        # a LAN connection out through the school proxy and fail.
        async with websockets.connect(url, max_size=32 * 1024 * 1024, proxy=None) as conn:
            self._conn = conn
            await self._send(
                conn,
                HelloPayload(
                    agent_id=self._id.agent_id,
                    hostname=self._id.hostname,
                    username=self._id.username,
                    agent_version=self._id.agent_version,
                    os_version=self._id.os_version,
                    cached_rule_revision=self._cached_rev(),
                    # Only sent while we have no token of our own yet.
                    pairing_code=None if self._token else self._pairing_code,
                ),
            )
            welcome = await self._await_welcome(conn)
            if welcome is None:
                return
            if welcome.agent_token:
                # Pairing succeeded: adopt and persist our own credential, so
                # the short pairing code is never needed again.
                self._token = welcome.agent_token
                self._pairing_code = None
                if self._on_token_issued is not None:
                    try:
                        self._on_token_issued(welcome.agent_token)
                    except Exception:
                        log.exception("could not store the issued agent token")
                log.info("paired with the dashboard; credential stored")

            # Mark connected before applying rules: the socket is live, so a
            # sweep triggered by the initial ruleset can already send events.
            self._hb_interval = welcome.heartbeat_interval_s
            self.connected.set()
            log.info("joined session %s", welcome.session_id)
            await self._on_ruleset(welcome.ruleset)

            hb_task = asyncio.ensure_future(self._heartbeat_loop(conn))
            try:
                await self._receive_loop(conn)
            finally:
                hb_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await hb_task

    async def _await_welcome(self, conn: websockets.ClientConnection) -> WelcomePayload | None:
        try:
            raw = await asyncio.wait_for(conn.recv(), timeout=10)
        except (TimeoutError, websockets.ConnectionClosed):
            return None
        env = decode(raw)
        if isinstance(env.payload, RejectPayload):
            reason = env.payload.reason
            log.error("dashboard rejected agent: %s", reason)
            if self._token is None and self._pairing_code:
                # A refused pairing code does not become right by retrying, and
                # every retry counts as a guess toward the dashboard's lockout
                # of this PC. Stop and let the person installing see why.
                self.pairing_rejected = reason
                self._stop.set()
            # Otherwise (e.g. a revoked token) keep retrying with backoff: the
            # admin may re-pair or restore the dashboard database.
            raise ProtocolError(f"rejected: {reason}")
        if isinstance(env.payload, WelcomePayload):
            return env.payload
        return None

    async def _receive_loop(self, conn: websockets.ClientConnection) -> None:
        async for raw in conn:
            try:
                env = decode(raw)
            except ProtocolError as exc:
                log.warning("dropping bad message: %s", exc)
                continue
            payload = env.payload
            if payload.type == MsgType.RULES_UPDATE and isinstance(payload, RulesUpdatePayload):
                await self._on_ruleset(payload.ruleset)
            elif payload.type == MsgType.COMMAND and isinstance(payload, CommandPayload):
                result = await self._on_command(payload)
                await self._send(
                    conn,
                    AckPayload(
                        agent_id=self._id.agent_id,
                        ack_id=env.id,
                        ok=result.ok,
                        resulting_state=result.resulting_state,
                        message=result.message,
                    ),
                )
            else:
                log.debug("ignoring %s", payload.type)

    async def _heartbeat_loop(self, conn: websockets.ClientConnection) -> None:
        while not self._stop.is_set():
            cpu, mem, title = self._metrics()
            with contextlib.suppress(websockets.ConnectionClosed):
                await self._send(
                    conn,
                    HeartbeatPayload(
                        agent_id=self._id.agent_id,
                        state=self._state(),
                        rule_revision=self._cached_rev(),
                        cpu_percent=cpu,
                        mem_mb=mem,
                        active_window_title=title,
                        indicator_visible=self._indicator_visible(),
                        desktop_access=self._desktop_access(),
                        username=self._username(),
                    ),
                )
            await self._sleep(self._hb_interval)

    async def send_event(self, payload: object) -> bool:
        """Push an agent-originated event (violation/frame) over the live link.

        Returns False if currently disconnected, so callers can drop or queue.
        """
        conn = self._conn
        if conn is None or not self.connected.is_set():
            return False
        try:
            await self._send(conn, payload)
            return True
        except websockets.ConnectionClosed:
            return False

    async def _send(self, conn: websockets.ClientConnection, payload: object) -> None:
        await conn.send(encode(payload, token=self._token))  # type: ignore[arg-type]

    async def stop(self) -> None:
        self._stop.set()
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._send(self._conn, GoodbyePayload(agent_id=self._id.agent_id))
                await self._conn.close()

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
