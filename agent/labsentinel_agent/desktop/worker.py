"""The desktop worker: runs inside the participant's session, answers the service.

    labsentinel-agent --desktop-worker        (started by the service, see spawn.py)

It owns nothing and decides nothing. It only reports what is on the desktop
(windows, the foreground title, a screenshot) and carries out two desktop
actions the service may ask for (close a program's windows, lock the screen).
Every rule, every decision and every record stays in the service.

The service starts it as SYSTEM in the active console session, so a participant
cannot end it; if it dies anyway the service starts another one, and while none
is connected the dashboard is told that desktop access is missing.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import random
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("labsentinel.agent.desktop.worker")

MAX_LINE = 1024 * 1024
GIVE_UP_AFTER_S = 120.0  # no service to talk to: exit, the service starts a new one


class DesktopOps(Protocol):
    def window_index(self) -> dict[int, str]: ...
    def foreground(self) -> tuple[str, str]: ...
    def capture_jpeg(self, *, max_width: int, quality: int) -> bytes | None: ...
    def close_windows(self, pid: int) -> bool: ...
    def lock(self) -> bool: ...
    def session_id(self) -> int: ...


class LocalDesktopOps:
    """The real desktop, from inside the session (Windows APIs; no-ops elsewhere)."""

    def __init__(self) -> None:
        from labsentinel_agent.capture.screenshot import MssScreenshotCapturer

        self._capturer = MssScreenshotCapturer()

    def window_index(self) -> dict[int, str]:
        from labsentinel_agent.platform.win_process import local_window_index

        return local_window_index()

    def foreground(self) -> tuple[str, str]:
        from labsentinel_agent.platform.win_window import foreground_process_and_title

        return foreground_process_and_title()

    def capture_jpeg(self, *, max_width: int, quality: int) -> bytes | None:
        return self._capturer.capture_jpeg(max_width=max_width, quality=quality)

    def close_windows(self, pid: int) -> bool:
        from labsentinel_agent.platform.win_process import post_wm_close

        return post_wm_close(pid)

    def lock(self) -> bool:
        from labsentinel_agent.platform.win_window import lock_workstation

        return lock_workstation()

    def session_id(self) -> int:
        from labsentinel_agent.platform.win_process import current_session_id

        return current_session_id()


class DesktopWorker:
    def __init__(
        self,
        *,
        ops: DesktopOps,
        endpoint_file: Path,
        token_file: Path,
        host: str = "127.0.0.1",
        give_up_after_s: float = GIVE_UP_AFTER_S,
    ) -> None:
        self._ops = ops
        self._endpoint_file = endpoint_file
        self._token_file = token_file
        self._host = host
        self._give_up_after = give_up_after_s
        self._stop = asyncio.Event()
        self._writer: asyncio.StreamWriter | None = None
        self.exit_reason = ""

    def stop(self) -> None:
        """Stop serving (call on the worker's loop). Wakes a pending read."""
        self._stop.set()
        if self._writer is not None:
            self._writer.close()

    async def run(self) -> str:
        """Serve until told to leave, stopped, or the service is gone for good."""
        loop = asyncio.get_running_loop()
        last_contact = loop.time()
        attempt = 0
        while not self._stop.is_set():
            try:
                reason = await self._session()
                if reason:  # the service said goodbye ("replaced", "stopping")
                    self.exit_reason = reason
                    return reason
                last_contact = loop.time()
                attempt = 0
            except (OSError, ValueError, asyncio.IncompleteReadError) as exc:
                log.debug("desktop worker link failed: %s", exc)
            if loop.time() - last_contact > self._give_up_after:
                self.exit_reason = "service unreachable"
                return self.exit_reason
            attempt += 1
            delay = min(5.0, 0.25 * 2 ** min(attempt, 5)) + random.uniform(0, 0.2)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
        self.exit_reason = "stopped"
        return self.exit_reason

    async def _session(self) -> str:
        port = int(self._endpoint_file.read_text(encoding="utf-8").strip())
        token = self._token_file.read_text(encoding="utf-8").strip()
        reader, writer = await asyncio.open_connection(self._host, port, limit=MAX_LINE)
        self._writer = writer
        try:
            await _send(writer, {
                "type": "hello", "token": token, "pid": os.getpid(),
                "session": await asyncio.to_thread(self._ops.session_id),
            })
            welcome = json.loads(await asyncio.wait_for(reader.readline(), timeout=5))
            if welcome.get("type") != "welcome":
                raise ValueError("not welcomed")
            log.info("serving desktop requests for the service")
            while not self._stop.is_set():
                raw = await reader.readline()
                if not raw:
                    return ""  # connection dropped: reconnect
                msg = json.loads(raw)
                if msg.get("type") == "bye":
                    return str(msg.get("reason") or "bye")
                if "op" in msg:
                    await _send(writer, await self._answer(msg))
            return ""
        finally:
            self._writer = None
            with contextlib.suppress(Exception):
                writer.close()

    async def _answer(self, msg: dict[str, Any]) -> dict[str, Any]:
        req_id = msg.get("id")
        op = str(msg.get("op"))
        args = msg.get("args") or {}
        try:
            result = await asyncio.to_thread(self._do, op, args)
        except Exception as exc:
            log.warning("desktop op %s failed", op, exc_info=True)
            return {"id": req_id, "ok": False, "error": type(exc).__name__}
        return {"id": req_id, "ok": True, "result": result}

    def _do(self, op: str, args: dict[str, Any]) -> Any:
        if op == "windows":
            return {str(pid): title for pid, title in self._ops.window_index().items()}
        if op == "foreground":
            return list(self._ops.foreground())
        if op == "capture":
            jpeg = self._ops.capture_jpeg(
                max_width=int(args.get("max_width", 1280)), quality=int(args.get("quality", 60))
            )
            return base64.b64encode(jpeg).decode("ascii") if jpeg else None
        if op == "close":
            return self._ops.close_windows(int(args["pid"]))
        if op == "lock":
            return self._ops.lock()
        if op == "ping":
            return "pong"
        raise ValueError(f"unknown op {op!r}")


async def _send(writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
    writer.write(json.dumps(msg).encode("utf-8") + b"\n")
    await writer.drain()


def run_worker(data_dir: Path) -> int:
    """Entry point for ``labsentinel-agent --desktop-worker``."""
    worker = DesktopWorker(
        ops=LocalDesktopOps(),
        endpoint_file=data_dir / "desktop_endpoint",
        token_file=data_dir / "desktop_token",
    )
    reason = asyncio.run(worker.run())
    log.info("desktop worker exiting: %s", reason)
    return 0
