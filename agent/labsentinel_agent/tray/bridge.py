"""Service-side half of the tray channel.

``TrayBridge`` listens on loopback, authenticates helper processes with a shared
token, and broadcasts status/warnings/notifications to them. It implements the
``WarningPresenter`` port, so the enforcement engine warns participants through
it without knowing anything about tray icons or IPC.

Security posture
----------------
* Binds to 127.0.0.1 only — never reachable from the network.
* Helpers authenticate with a token file written by the service; the token is
  never logged.
* Inbound messages other than ``hello``/``alive`` are **ignored**. The helper
  has no authority to change enforcement, so a participant who replaces it
  cannot pause monitoring.
* ``indicator_visible`` reports whether any helper is currently showing the
  icon, so the dashboard can flag a PC whose participant killed the indicator.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import stat
import time
from pathlib import Path

from pydantic import BaseModel

from labsentinel_agent.tray.ipc import (
    AliveMsg,
    HelloMsg,
    IpcError,
    NotifyMsg,
    StatusMsg,
    WarnMsg,
    decode,
    encode,
)
from labsentinel_shared.enums import AgentState

log = logging.getLogger("labsentinel.agent.tray")

HELPER_TIMEOUT_S = 30.0


class TrayBridge:
    def __init__(
        self,
        *,
        token_file: Path,
        endpoint_file: Path,
        host: str = "127.0.0.1",
        port: int = 0,
        dashboard_name: str = "Lab Sentinel",
    ) -> None:
        self._token_file = token_file
        self._endpoint_file = endpoint_file
        self._host = host
        self._port = port
        self._dashboard_name = dashboard_name
        self._token = ""
        self._server: asyncio.base_events.Server | None = None
        self._clients: set[asyncio.StreamWriter] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._state = AgentState.ENFORCING
        self._connected = False
        self._last_alive = 0.0

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._token = self._provision_token()
        self._server = await asyncio.start_server(self._handle, self._host, self._port)
        self._write_endpoint(self.bound_port)
        log.info("tray bridge listening on %s:%d", self._host, self.bound_port)

    async def stop(self) -> None:
        for writer in list(self._clients):
            with contextlib.suppress(Exception):
                writer.close()
        self._clients.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(OSError):
            self._endpoint_file.unlink()

    @property
    def bound_port(self) -> int:
        if self._server and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self._port

    @property
    def indicator_visible(self) -> bool:
        """True while at least one helper is showing the icon."""
        if not self._clients:
            return False
        now = self._now()
        return (now - self._last_alive) <= HELPER_TIMEOUT_S

    # -- outbound ----------------------------------------------------------
    def set_status(self, state: AgentState, *, connected: bool) -> None:
        """Record and broadcast the agent's current status."""
        self._state = state
        self._connected = connected
        self._broadcast(
            StatusMsg(state=state, connected=connected, dashboard_name=self._dashboard_name)
        )

    # WarningPresenter port -------------------------------------------------
    def warn_closing(self, process_name: str, seconds: int, reason: str) -> None:
        self._broadcast(WarnMsg(app=process_name, seconds=seconds, reason=reason[:512]))

    def notify(self, title: str, message: str) -> None:
        self._broadcast(NotifyMsg(title=title[:128], message=message[:1024]))

    # -- internals ---------------------------------------------------------
    def _broadcast(self, msg: BaseModel) -> None:
        """Send to every helper. Safe to call from the agent's asyncio loop."""
        if not self._clients:
            return
        data = encode(msg)
        for writer in list(self._clients):
            try:
                writer.write(data)
            except Exception:
                self._drop(writer)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        authenticated = False
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = decode(line)
                except IpcError as exc:
                    log.warning("dropping bad ipc line from %s: %s", peer, exc)
                    continue

                if isinstance(msg, HelloMsg):
                    if not secrets.compare_digest(msg.token, self._token):
                        log.warning("tray helper from %s failed authentication", peer)
                        break
                    authenticated = True
                    self._clients.add(writer)
                    self._last_alive = self._now()
                    log.info("tray helper connected (pid=%d user=%s)", msg.pid, msg.username)
                    # Send current status immediately so the icon is correct.
                    writer.write(
                        encode(
                            StatusMsg(
                                state=self._state,
                                connected=self._connected,
                                dashboard_name=self._dashboard_name,
                            )
                        )
                    )
                elif isinstance(msg, AliveMsg):
                    if authenticated:
                        self._last_alive = self._now()
                else:
                    # Display-only channel: the helper has no authority here.
                    log.debug("ignoring non-authoritative ipc message %s", msg.kind)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.warning("tray helper connection error", exc_info=True)
        finally:
            self._drop(writer)

    def _drop(self, writer: asyncio.StreamWriter) -> None:
        self._clients.discard(writer)
        with contextlib.suppress(Exception):
            writer.close()

    def _provision_token(self) -> str:
        """Create (or reuse) the token the helper must present."""
        try:
            existing = self._token_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        except OSError:
            pass
        token = secrets.token_urlsafe(32)
        self._token_file.parent.mkdir(parents=True, exist_ok=True)
        self._token_file.write_text(token, encoding="utf-8")
        with contextlib.suppress(OSError):
            # Readable by local users (the helper runs as the participant),
            # writable only by the owner (the service).
            os.chmod(self._token_file, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        return token

    def _write_endpoint(self, port: int) -> None:
        self._endpoint_file.parent.mkdir(parents=True, exist_ok=True)
        self._endpoint_file.write_text(str(port), encoding="utf-8")

    @staticmethod
    def _now() -> float:
        return time.monotonic()
