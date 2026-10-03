"""M3 capstone: real pause/resume control loop over WebSockets.

Wires the real DashboardService + WebSocketServer to a real agent (AgentCore +
DashboardClient + engine) and proves: an admin pause reaches the agent, is
ACKed with the resulting state, flips the dashboard's view to paused, is audited
and recorded in the DB; resume brings enforcement back; and the desired-pause
state is re-applied when the agent reconnects.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from labsentinel_agent.core import AgentCore, RuntimeState, _Detector
from labsentinel_agent.enforce.engine import EnforcementEngine, ViolationRecord
from labsentinel_agent.enforce.matcher import WhitelistMatcher
from labsentinel_agent.enforce.safety import SafetyPolicy
from labsentinel_agent.rule_cache import RuleCache
from labsentinel_agent.transport.client import AgentIdentity, BackoffPolicy, DashboardClient
from labsentinel_dashboard.domain.events import EventBus
from labsentinel_dashboard.domain.rules_service import RulesService
from labsentinel_dashboard.domain.service import CommandOutcome, DashboardService
from labsentinel_dashboard.storage.db import Database
from labsentinel_dashboard.transport.server import WebSocketServer
from labsentinel_shared.enums import AgentState, ConnectionStatus, EnforcementMode
from labsentinel_shared.rules import RuleSet
from labsentinel_shared.security import hash_token, verify_token

pytestmark = pytest.mark.integration
TOKEN = "m3-token"


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


def _build_agent(
    url: str, runtime: RuntimeState, cache: RuleCache
) -> tuple[AgentCore, DashboardClient]:
    box: dict[str, AgentCore] = {}

    async def emit(rec: ViolationRecord) -> None:
        await box["core"].emit_violation(rec)

    async def no_sleep(_: float) -> None:
        return None

    engine = EnforcementEngine(
        safety=SafetyPolicy(), matcher=WhitelistMatcher(), controller=FakeController(),
        warner=FakeWarner(), capturer=None, emit=emit,
        state_provider=lambda: runtime.state, ruleset_provider=runtime.effective_ruleset,
        sleep=no_sleep,
    )
    detector = _Detector(
        watch_new_processes=lambda cb: asyncio.sleep(0),
        list_processes=lambda: [], stop=lambda: None,
    )
    core = AgentCore(runtime=runtime, cache=cache, engine=engine, detector=detector,
                     lock_fn=lambda: True, notify_fn=lambda t, m: None)
    box["core"] = core
    client = DashboardClient(
        identity=AgentIdentity(agent_id="pc-core", hostname="LAB-CORE"),
        token=TOKEN, state_provider=core.state,
        metrics_provider=lambda: (1.0, 40.0, "Chrome"),
        on_ruleset=core.on_ruleset, on_command=core.on_command,
        heartbeat_interval_s=0.3,
        backoff=BackoffPolicy(base_s=0.1, max_s=0.2, jitter_s=0.0),
    )
    core.attach_client(client)
    return core, client


async def _wait(pred, timeout: float = 5.0) -> None:
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


@pytest.mark.asyncio
async def test_pause_resume_control_loop(tmp_path: Path) -> None:
    db = Database(tmp_path / "m3.sqlite")
    bus = EventBus()
    rules = RulesService(db, default=RuleSet(revision=1, mode=EnforcementMode.LAB, dry_run=True))
    service = DashboardService(
        db=db, bus=bus, rules_provider=rules.current,
        screenshot_dir=tmp_path / "shots", command_timeout_s=3.0, heartbeat_timeout_s=5.0,
    )
    server = WebSocketServer(
        host="127.0.0.1", port=0,
        token_verifier=lambda t: verify_token(t, hash_token(TOKEN)),
        ruleset_provider=rules.ruleset_for, events=service, reply_token=TOKEN,
        heartbeat_interval_s=0.3,
    )
    service.attach(server)
    await server.start()
    url = f"ws://127.0.0.1:{server.bound_port}"

    runtime = RuntimeState(state=AgentState.ENFORCING)
    cache = RuleCache(tmp_path / "rules.json")
    _core, client = _build_agent(url, runtime, cache)
    run_task = asyncio.ensure_future(client.run(lambda: _ret(url)))

    try:
        await asyncio.wait_for(client.connected.wait(), timeout=5)
        await _wait(lambda: service.agent("pc-core") is not None
                    and service.agent("pc-core").connection is ConnectionStatus.ONLINE)

        # --- PAUSE from the dashboard ---
        report = await service.pause(["pc-core"], actor="dosen")
        assert report.outcomes["pc-core"] is CommandOutcome.ACKED
        # Agent really paused, and the dashboard view reflects the real state.
        assert runtime.state is AgentState.PAUSED
        assert service.agent("pc-core").agent_state is AgentState.PAUSED
        assert service.agent("pc-core").status is ConnectionStatus.PAUSED
        # Audited + recorded.
        actions = [a.action for a in db.list_audit()]
        assert "command.pause" in actions and "command.pause.hasil" in actions
        cmds = db.recent_commands(agent_id="pc-core")
        assert cmds and cmds[0]["command"] == "pause" and cmds[0]["status"] == "acked"

        # --- RESUME ---
        report = await service.resume(["pc-core"], actor="dosen")
        assert report.outcomes["pc-core"] is CommandOutcome.ACKED
        assert runtime.state is AgentState.ENFORCING

        # --- desired-pause survives an agent RESTART ---
        await service.pause(["pc-core"], actor="dosen")
        assert runtime.state is AgentState.PAUSED
        # Simulate the agent process restarting: stop it, then start a fresh one
        # (new runtime defaults to ENFORCING) with the same agent_id.
        await client.stop()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(run_task, timeout=5)
        await _wait(lambda: service.agent("pc-core").connection
                    in (ConnectionStatus.OFFLINE, ConnectionStatus.DROPPED))

        runtime2 = RuntimeState(state=AgentState.ENFORCING)
        cache2 = RuleCache(tmp_path / "rules2.json")
        _core2, client2 = _build_agent(url, runtime2, cache2)
        run_task2 = asyncio.ensure_future(client2.run(lambda: _ret(url)))
        try:
            await asyncio.wait_for(client2.connected.wait(), timeout=5)
            # Dashboard re-applies the admin's pause to the restarted agent.
            await _wait(lambda: runtime2.state is AgentState.PAUSED, timeout=5)
        finally:
            await client2.stop()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(run_task2, timeout=5)
            if not run_task2.done():
                run_task2.cancel()
    finally:
        await client.stop()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(run_task, timeout=5)
        if not run_task.done():
            run_task.cancel()
        await server.stop()
        db.close()


async def _ret(url: str) -> str:
    return url
