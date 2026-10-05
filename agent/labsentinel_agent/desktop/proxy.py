"""Service side of desktop access: talk to the desktop worker in the user's session.

A Windows Service lives in session 0 and cannot see the participant's desktop:
no window list, no foreground title, no screenshot, no WM_CLOSE, no lock. The
service therefore starts a small *desktop worker* inside the active session
(see spawn.py) which connects back here and answers those questions.

Why its own thread and event loop: the enforcement code asks synchronous
questions ("which windows exist?", "capture the screen") from the agent's main
loop and from executor threads. Serving the worker on a separate loop means a
blocking call from anywhere, including the main loop, can never deadlock.

Trust: the worker must present a token from a file only SYSTEM and
Administrators can read, so a participant cannot stand in for it with fake
screenshots. One worker at a time; a newer one replaces the older, which is
told to exit.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import itertools
import json
import logging
import os
import secrets
import tempfile
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any

log = logging.getLogger("labsentinel.agent.desktop")

MAX_LINE = 16 * 1024 * 1024  # a 1280px JPEG in base64 is ~150 KB; plenty of room
HELLO_TIMEOUT_S = 5.0


class DesktopProxy:
    def __init__(
        self, *, endpoint_file: Path, token_file: Path, host: str = "127.0.0.1"
    ) -> None:
        self._endpoint_file = endpoint_file
        self._token_file = token_file
        self._host = host
        self._token = ""
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: asyncio.base_events.Server | None = None
        self._ready = threading.Event()
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._ids = itertools.count(1)
        self.session_id: int | None = None
        self.worker_pid: int | None = None
        self.port = 0

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._token = _provision_token(self._token_file)
        self._thread = threading.Thread(target=self._main, name="ls-desktop", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise RuntimeError("desktop proxy did not start")

    def stop(self) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with contextlib.suppress(RuntimeError):
            asyncio.run_coroutine_threadsafe(self._shutdown(), loop).result(timeout=5)
        if self._thread is not None:
            self._thread.join(timeout=5)
        with contextlib.suppress(OSError):
            self._endpoint_file.unlink()

    def _main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
            self._ready.set()
            loop.run_forever()
        finally:
            self._ready.set()
            with contextlib.suppress(Exception):
                loop.close()

    async def _serve(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self._host, 0, limit=MAX_LINE
        )
        self.port = int(self._server.sockets[0].getsockname()[1])
        _write_private(self._endpoint_file, str(self.port))
        log.info("desktop access listening on %s:%d", self._host, self.port)

    async def _shutdown(self) -> None:
        await self._drop_worker("stopping")
        if self._server is not None:
            self._server.close()
        loop = asyncio.get_running_loop()
        others = [t for t in asyncio.all_tasks(loop) if t is not asyncio.current_task()]
        for task in others:
            task.cancel()
        await asyncio.gather(*others, return_exceptions=True)
        loop.call_soon(loop.stop)

    # ------------------------------------------------------------- workers
    @property
    def connected(self) -> bool:
        return self._writer is not None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=HELLO_TIMEOUT_S)
            hello = json.loads(line)
            presented = str(hello.get("token", ""))
            if hello.get("type") != "hello" or not hmac.compare_digest(presented, self._token):
                log.warning("refused a desktop worker with a wrong token")
                writer.close()
                return
        except (TimeoutError, ValueError, OSError, asyncio.IncompleteReadError):
            writer.close()
            return

        await self._drop_worker("replaced")  # newest worker wins; the old one exits
        self._writer = writer
        self.session_id = _int_or_none(hello.get("session"))
        self.worker_pid = _int_or_none(hello.get("pid"))
        await self._send(writer, {"type": "welcome"})
        log.info("desktop worker connected (session %s, pid %s)", self.session_id, self.worker_pid)
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                with contextlib.suppress(ValueError):
                    self._resolve(json.loads(raw))
        except (OSError, asyncio.LimitOverrunError, ValueError):
            log.debug("desktop worker connection error", exc_info=True)
        finally:
            if self._writer is writer:
                self._writer = None
                self.session_id = None
                self.worker_pid = None
                self._fail_pending()
                log.info("desktop worker disconnected")
            with contextlib.suppress(Exception):
                writer.close()

    async def _drop_worker(self, reason: str) -> None:
        writer, self._writer = self._writer, None
        self._fail_pending()
        if writer is None:
            return
        with contextlib.suppress(Exception):
            await self._send(writer, {"type": "bye", "reason": reason})
            writer.close()

    def _resolve(self, msg: dict[str, Any]) -> None:
        fut = self._pending.pop(_int_or_none(msg.get("id")) or -1, None)
        if fut is not None and not fut.done():
            fut.set_result(msg)

    def _fail_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result({"ok": False, "error": "worker gone"})
        self._pending.clear()

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
        writer.write(json.dumps(msg).encode("utf-8") + b"\n")
        await writer.drain()

    # ------------------------------------------------------------- requests
    async def _request(self, op: str, args: dict[str, Any], timeout: float) -> Any:
        writer = self._writer
        if writer is None:
            return None
        req_id = next(self._ids)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        try:
            await self._send(writer, {"id": req_id, "op": op, "args": args})
            reply = await asyncio.wait_for(fut, timeout=timeout)
        except (TimeoutError, OSError):
            log.warning("desktop worker did not answer %s in %.1fs", op, timeout)
            return None
        finally:
            self._pending.pop(req_id, None)
        if not reply.get("ok"):
            log.debug("desktop worker could not %s: %s", op, reply.get("error"))
            return None
        return reply.get("result")

    def request(self, op: str, args: dict[str, Any] | None = None, *, timeout: float = 2.0) -> Any:
        """Ask the worker something, from any thread. None if unavailable."""
        loop = self._loop
        if loop is None or loop.is_closed() or self._writer is None:
            return None
        if threading.current_thread() is self._thread:
            raise RuntimeError("DesktopProxy.request must not be called from its own loop")
        future: Future[Any] = asyncio.run_coroutine_threadsafe(
            self._request(op, args or {}, timeout), loop
        )
        try:
            return future.result(timeout=timeout + 1.0)
        except Exception:
            future.cancel()
            return None

    # ------------------------------------------------- typed conveniences
    def window_index(self) -> dict[int, str] | None:
        result = self.request("windows", timeout=1.5)
        if not isinstance(result, dict):
            return None
        return {int(pid): str(title) for pid, title in result.items()}

    def foreground(self) -> tuple[str, str] | None:
        result = self.request("foreground", timeout=1.0)
        if isinstance(result, list) and len(result) == 2:
            return str(result[0]), str(result[1])
        return None

    def capture_jpeg(self, *, max_width: int = 1280, quality: int = 60) -> bytes | None:
        result = self.request(
            "capture", {"max_width": max_width, "quality": quality}, timeout=4.0
        )
        if not isinstance(result, str):
            return None
        try:
            return base64.b64decode(result)
        except ValueError:
            return None

    def close_windows(self, pid: int) -> bool | None:
        result = self.request("close", {"pid": int(pid)}, timeout=2.0)
        return None if result is None else bool(result)

    def lock(self) -> bool | None:
        result = self.request("lock", timeout=2.0)
        return None if result is None else bool(result)

    def freeze(self, on: bool) -> bool | None:
        # Releasing joins the hold thread (up to ~3s), so allow for that here.
        result = self.request("freeze", {"on": bool(on)}, timeout=4.0)
        return None if result is None else bool(result)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _write_private(path: Path, text: str) -> None:
    """Atomic write, owner-only on POSIX, SYSTEM+Administrators on Windows."""
    from labsentinel_agent.platform.acl import restrict_to_admins

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".desk-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    restrict_to_admins(path)


def _provision_token(path: Path) -> str:
    """A fresh secret per service start; the worker reads it from disk."""
    token = secrets.token_urlsafe(32)
    _write_private(path, token)
    return token
