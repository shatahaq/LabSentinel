"""User-session half of the tray channel.

Connects to the service's ``TrayBridge``, authenticates with the shared token,
then applies incoming status/warning/notification messages to a
``TrayIndicator``. Sends periodic ``alive`` messages so the service knows the
indicator is actually on screen.

If the service is not up yet, or the connection drops, the helper shows a
"disconnected" indicator and retries with backoff — the participant is never
left looking at a stale "monitoring active" icon.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import random
from pathlib import Path

from labsentinel_agent.tray.icon import TrayIndicator
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

log = logging.getLogger("labsentinel.agent.tray.helper")


class TrayHelper:
    def __init__(
        self,
        *,
        indicator: TrayIndicator,
        token_file: Path,
        endpoint_file: Path,
        host: str = "127.0.0.1",
        alive_interval_s: float = 10.0,
        retry_base_s: float = 1.0,
        retry_max_s: float = 15.0,
    ) -> None:
        self._indicator = indicator
        self._token_file = token_file
        self._endpoint_file = endpoint_file
        self._host = host
        self._alive_interval = alive_interval_s
        self._retry_base = retry_base_s
        self._retry_max = retry_max_s
        self._stop = asyncio.Event()
        self._state = AgentState.ENFORCING

    async def run(self) -> None:
        """Connect/reconnect until stopped."""
        attempt = 0
        # Until we have heard from the service, show "disconnected".
        self._indicator.set_status(self._state, connected=False)
        while not self._stop.is_set():
            endpoint = self._read_endpoint()
            token = self._read_token()
            if endpoint is None or token is None:
                await self._wait(self._delay(attempt + 1))
                attempt += 1
                continue
            try:
                await self._session(endpoint, token)
                attempt = 0
            except (OSError, IpcError, asyncio.IncompleteReadError) as exc:
                log.info("tray helper connection failed: %s", exc)
            except Exception:
                log.exception("unexpected tray helper error")
            finally:
                # Connection is gone: stop claiming monitoring is active.
                self._indicator.set_status(self._state, connected=False)
            if self._stop.is_set():
                break
            attempt += 1
            await self._wait(self._delay(attempt))

    async def _session(self, port: int, token: str) -> None:
        reader, writer = await asyncio.open_connection(self._host, port)
        try:
            writer.write(
                encode(HelloMsg(token=token, pid=os.getpid(), username=_current_user()))
            )
            await writer.drain()
            alive = asyncio.ensure_future(self._alive_loop(writer))
            try:
                while not self._stop.is_set():
                    line = await reader.readline()
                    if not line:
                        break
                    try:
                        msg = decode(line)
                    except IpcError as exc:
                        log.warning("dropping bad ipc line: %s", exc)
                        continue
                    self._apply(msg)
            finally:
                alive.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await alive
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _apply(self, msg: object) -> None:
        if isinstance(msg, StatusMsg):
            self._state = msg.state
            self._indicator.set_status(msg.state, connected=msg.connected)
        elif isinstance(msg, WarnMsg):
            self._indicator.show_warning(msg.app, msg.seconds, msg.reason)
        elif isinstance(msg, NotifyMsg):
            self._indicator.show_message(msg.title, msg.message)
        # Anything else is not meant for the helper; ignore it.

    async def _alive_loop(self, writer: asyncio.StreamWriter) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self._alive_interval)
            try:
                writer.write(encode(AliveMsg()))
                await writer.drain()
            except Exception:
                return

    def stop(self) -> None:
        self._stop.set()
        self._indicator.stop()

    def _delay(self, attempt: int) -> float:
        raw = min(self._retry_max, self._retry_base * (2 ** max(0, attempt - 1)))
        return float(raw + random.uniform(0, 0.3))

    async def _wait(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    def _read_endpoint(self) -> int | None:
        try:
            port = int(self._endpoint_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None
        return port if 0 < port < 65536 else None

    def _read_token(self) -> str | None:
        try:
            token = self._token_file.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return token or None


def _current_user() -> str:
    try:
        return os.getlogin()
    except OSError:
        return os.environ.get("USERNAME", "")
