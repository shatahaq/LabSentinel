"""M2/M3 capstone: AgentCore end-to-end with real transport over loopback.

Proves: WELCOME ruleset triggers a sweep of running apps, a non-whitelisted app
produces a (dry-run) ViolationPayload with screenshot evidence at the dashboard,
PAUSE halts enforcement, and RESUME re-sweeps and enforces.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from labsentinel_agent.core import AgentCore, RuntimeState, _Detector
from labsentinel_agent.enforce.engine import EnforcementEngine, ViolationRecord
from labsentinel_agent.enforce.identity import ProcessInfo
from labsentinel_agent.enforce.matcher import WhitelistMatcher
from labsentinel_agent.enforce.safety import SafetyPolicy
from labsentinel_agent.rule_cache import RuleCache
from labsentinel_agent.transport.client import AgentIdentity, BackoffPolicy, DashboardClient
from labsentinel_dashboard.transport.server import AgentSession, WebSocketServer
from labsentinel_shared.enums import AgentState, CommandType
from labsentinel_shared.protocol import (
    AckPayload,
    CommandPayload,
    HeartbeatPayload,
    ScreenFramePayload,
    ViolationPayload,
)
from labsentinel_shared.rules import AppRule, RuleSet
from labsentinel_shared.security import hash_token, verify_token

pytestmark = pytest.mark.integration

TOKEN = "core-test-token"


class FakeController:
    async def graceful_close(self, pid: int, timeout_s: float) -> bool:
        return True

    async def force_kill(self, pid: int) -> bool:
        return True

    def is_running(self, pid: int) -> bool:
        return True


class FakeWarner:
    def warn_closing(self, process_name: str, seconds: int, reason: str) -> None: ...
    def notify(self, title: str, message: str) -> None: ...


class FakeCapturer:
    def capture_jpeg(self, *, max_width: int = 1280, quality: int = 60) -> bytes | None:
        return b"\xff\xd8\xff-evidence"


class Events:
    def __init__(self) -> None:
        self.violations: list[ViolationPayload] = []
        self.acks: list[AckPayload] = []
        self._v = asyncio.Event()
        self._a = asyncio.Event()

    async def on_agent_connected(self, s: AgentSession) -> None: ...
    async def on_agent_disconnected(self, agent_id: str, *, clean: bool) -> None: ...
    async def on_heartbeat(self, agent_id: str, hb: HeartbeatPayload) -> None: ...
    async def on_screen_frame(self, agent_id: str, f: ScreenFramePayload) -> None: ...

    async def on_violation(self, agent_id: str, v: ViolationPayload) -> None:
        self.violations.append(v)
        self._v.set()

    async def on_ack(self, agent_id: str, ack: AckPayload) -> None:
        self.acks.append(ack)
        self._a.set()

    async def wait_violation(self) -> None:
        await asyncio.wait_for(self._v.wait(), timeout=5)
        self._v.clear()

    async def wait_ack(self) -> None:
        await asyncio.wait_for(self._a.wait(), timeout=5)
        self._a.clear()


def _user_proc(pid: int, name: str) -> ProcessInfo:
    return ProcessInfo(
        pid=pid, name=name, exe_path=f"C:/Games/{name}",
        username="LAB\\student", session_id=1, is_current_user=True, has_visible_window=True,
    )


def _initial_procs() -> list[ProcessInfo]:
    return [
        _user_proc(10, "chrome.exe"),  # allowed (default whitelist)
        _user_proc(20, "game.exe"),  # violation
        ProcessInfo(pid=4, name="system", username="NT AUTHORITY\\SYSTEM",
                    is_current_user=False, has_visible_window=False),  # protected
    ]


@pytest.mark.asyncio
async def test_core_end_to_end(tmp_path) -> None:
    runtime = RuntimeState(state=AgentState.ENFORCING)
    cache = RuleCache(tmp_path / "rules.json")

    core_box: dict[str, AgentCore] = {}

    async def emit(rec: ViolationRecord) -> None:
        await core_box["core"].emit_violation(rec)

    async def no_sleep(_: float) -> None:
        return None

    engine = EnforcementEngine(
        safety=SafetyPolicy(),
        matcher=WhitelistMatcher(),
        controller=FakeController(),
        warner=FakeWarner(),
        capturer=FakeCapturer(),
        emit=emit,
        state_provider=lambda: runtime.state,
        ruleset_provider=runtime.effective_ruleset,
        sleep=no_sleep,
    )

    procs = _initial_procs()
    detector = _Detector(
        watch_new_processes=lambda cb: asyncio.sleep(0),  # no live events in this test
        list_processes=lambda: list(procs),
        stop=lambda: None,
    )
    core = AgentCore(
        runtime=runtime, cache=cache, engine=engine, detector=detector,
        lock_fn=lambda: True, notify_fn=lambda t, m: None,
    )
    core_box["core"] = core

    events = Events()
    # Initial ruleset: dry-run, game.exe NOT whitelisted (only defaults + code.exe).
    initial = RuleSet(revision=1, dry_run=True, app_whitelist=(AppRule(process_name="code.exe"),))
    server = WebSocketServer(
        host="127.0.0.1", port=0,
        token_verifier=lambda t: verify_token(t, hash_token(TOKEN)),
        ruleset_provider=lambda hello: initial,
        events=events, reply_token=TOKEN, heartbeat_interval_s=0.5,
    )
    await server.start()
    url = f"ws://127.0.0.1:{server.bound_port}"

    client = DashboardClient(
        identity=AgentIdentity(agent_id="pc-core", hostname="LAB-CORE"),
        token=TOKEN,
        state_provider=core.state,
        metrics_provider=lambda: (1.0, 40.0, "Chrome"),
        on_ruleset=core.on_ruleset,
        on_command=core.on_command,
        heartbeat_interval_s=0.5,
        backoff=BackoffPolicy(base_s=0.1, max_s=0.2, jitter_s=0.0),
    )
    core.attach_client(client)
    run_task = asyncio.ensure_future(client.run(lambda: _ret(url)))

    try:
        await asyncio.wait_for(client.connected.wait(), timeout=5)

        # WELCOME ruleset -> sweep -> game.exe is a dry-run violation with evidence.
        await events.wait_violation()
        assert len(events.violations) == 1
        v = events.violations[0]
        assert v.subject == "game.exe"
        assert v.dry_run is True
        assert v.screenshot_b64 is not None  # evidence attached
        assert cache.current is not None and cache.current.revision == 1

        # PAUSE -> no enforcement on a subsequent sweep.
        session = server.get("pc-core")
        assert session is not None
        await session.send_command(CommandPayload(command=CommandType.PAUSE, issued_by="dosen"))
        await events.wait_ack()
        assert events.acks[-1].resulting_state is AgentState.PAUSED
        before = len(events.violations)
        await core.sweep_running()  # paused: should do nothing
        await asyncio.sleep(0.2)
        assert len(events.violations) == before

        # A new violating app appears while paused (dedup won't suppress a new pid).
        procs.append(_user_proc(21, "game2.exe"))

        # RESUME -> re-sweep -> the new app is caught, proving enforcement is live.
        await session.send_command(CommandPayload(command=CommandType.RESUME, issued_by="dosen"))
        await events.wait_ack()
        assert events.acks[-1].resulting_state is AgentState.ENFORCING
        await events.wait_violation()
        assert events.violations[-1].subject == "game2.exe"
    finally:
        await client.stop()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(run_task, timeout=5)
        if not run_task.done():
            run_task.cancel()
        await server.stop()


async def _ret(url: str) -> str:
    return url
