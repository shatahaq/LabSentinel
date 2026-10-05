"""Full chain: dashboard pause -> agent -> participant's tray icon.

Proves the transparency requirement end to end: what the supervisor does is
reflected on the participant's screen within seconds, and if the participant
kills the indicator the dashboard is told.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from auth_helpers import SharedTokenAuth
from labsentinel_agent.core import AgentCore, RuntimeState, _Detector
from labsentinel_agent.enforce.engine import EnforcementEngine, ViolationRecord
from labsentinel_agent.enforce.matcher import WhitelistMatcher
from labsentinel_agent.enforce.safety import SafetyPolicy
from labsentinel_agent.rule_cache import RuleCache
from labsentinel_agent.transport.client import AgentIdentity, BackoffPolicy, DashboardClient
from labsentinel_agent.tray.bridge import TrayBridge
from labsentinel_agent.tray.helper import TrayHelper
from labsentinel_agent.tray.icon import NullIndicator
from labsentinel_dashboard.domain.events import EventBus
from labsentinel_dashboard.domain.rules_service import RulesService
from labsentinel_dashboard.domain.service import CommandOutcome, DashboardService
from labsentinel_dashboard.storage.db import Database
from labsentinel_dashboard.transport.server import WebSocketServer
from labsentinel_shared.enums import AgentState, EnforcementMode
from labsentinel_shared.rules import RuleSet

pytestmark = pytest.mark.integration
TOKEN = "tray-e2e-token"


class FakeController:
    async def graceful_close(self, pid: int, timeout_s: float) -> bool:
        return True

    async def force_kill(self, pid: int) -> bool:
        return True

    def is_running(self, pid: int) -> bool:
        return True


async def _wait(pred, timeout: float = 5.0) -> None:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


def last_status(ind: NullIndicator) -> tuple[AgentState, bool] | None:
    return ind.statuses[-1] if ind.statuses else None


@pytest.mark.asyncio
async def test_dashboard_pause_reaches_participant_icon(tmp_path: Path) -> None:
    # --- dashboard side ---
    db = Database(tmp_path / "e2e.sqlite")
    bus = EventBus()
    rules = RulesService(db, default=RuleSet(revision=1, mode=EnforcementMode.LAB, dry_run=True))
    service = DashboardService(
        db=db, bus=bus, rules_provider=rules.current,
        screenshot_dir=tmp_path / "shots", command_timeout_s=3.0,
    )
    server = WebSocketServer(
        host="127.0.0.1", port=0,
        authenticator=SharedTokenAuth(TOKEN),
        ruleset_provider=rules.ruleset_for, events=service, reply_token=TOKEN,
        heartbeat_interval_s=0.2,
    )
    service.attach(server)
    await server.start()
    url = f"ws://127.0.0.1:{server.bound_port}"

    # --- participant PC: service half (tray bridge) + agent core ---
    bridge = TrayBridge(
        token_file=tmp_path / "tray_token",
        endpoint_file=tmp_path / "tray_endpoint",
        port=0,
    )
    await bridge.start()

    runtime = RuntimeState(state=AgentState.ENFORCING)
    box: dict[str, AgentCore] = {}

    async def emit(rec: ViolationRecord) -> None:
        await box["core"].emit_violation(rec)

    async def no_sleep(_: float) -> None:
        return None

    engine = EnforcementEngine(
        safety=SafetyPolicy(), matcher=WhitelistMatcher(), controller=FakeController(),
        warner=bridge,  # the tray bridge IS the WarningPresenter
        capturer=None, emit=emit,
        state_provider=lambda: runtime.state, ruleset_provider=runtime.effective_ruleset,
        sleep=no_sleep,
    )
    core = AgentCore(
        runtime=runtime, cache=RuleCache(tmp_path / "rules.json"), engine=engine,
        detector=_Detector(watch_new_processes=lambda cb: asyncio.sleep(0),
                           list_processes=lambda: [], stop=lambda: None),
        lock_fn=lambda: True, notify_fn=bridge.notify,
        status_sink=lambda state, connected: bridge.set_status(state, connected=connected),
    )
    box["core"] = core

    client = DashboardClient(
        identity=AgentIdentity(agent_id="pc-tray", hostname="LAB-TRAY"),
        token=TOKEN, state_provider=core.state,
        metrics_provider=lambda: (1.0, 40.0, "Chrome"),
        on_ruleset=core.on_ruleset, on_command=core.on_command,
        indicator_visible_provider=lambda: bridge.indicator_visible,
        heartbeat_interval_s=0.2,
        backoff=BackoffPolicy(base_s=0.1, max_s=0.2, jitter_s=0.0),
    )
    core.attach_client(client)
    run_task = asyncio.ensure_future(client.run(lambda: _ret(url)))

    # --- participant PC: user-session half (tray helper) ---
    ind = NullIndicator()
    helper = TrayHelper(
        indicator=ind, token_file=tmp_path / "tray_token",
        endpoint_file=tmp_path / "tray_endpoint",
        alive_interval_s=0.05, retry_base_s=0.05, retry_max_s=0.2,
    )
    helper_task = asyncio.ensure_future(helper.run())

    try:
        await asyncio.wait_for(client.connected.wait(), timeout=5)
        core.set_connected(True)
        await _wait(lambda: last_status(ind) == (AgentState.ENFORCING, True))
        assert ind.last_tooltip == "Lab Sentinel — Pemantauan aktif"

        # Dashboard reports the indicator is visible to the participant.
        await _wait(lambda: service.agent("pc-tray") is not None
                    and service.agent("pc-tray").indicator_visible is True)

        # --- supervisor pauses monitoring ---
        report = await service.pause(["pc-tray"], actor="dosen")
        assert report.outcomes["pc-tray"] is CommandOutcome.ACKED

        # ...and the participant's icon says so.
        await _wait(lambda: last_status(ind) == (AgentState.PAUSED, True))
        assert ind.last_tooltip == "Lab Sentinel — Pemantauan dijeda"

        # --- supervisor resumes ---
        await service.resume(["pc-tray"], actor="dosen")
        await _wait(lambda: last_status(ind) == (AgentState.ENFORCING, True))

        # --- a warning from enforcement reaches the participant ---
        bridge.warn_closing("game.exe", 5, "tidak ada dalam whitelist")
        await _wait(lambda: bool(ind.warnings))
        assert ind.warnings[0][0] == "game.exe"

        # --- participant kills the indicator: dashboard is told ---
        helper.stop()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(helper_task, timeout=3)
        await _wait(lambda: service.agent("pc-tray").indicator_visible is False, timeout=5)
    finally:
        helper.stop()
        helper_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(helper_task, timeout=2)
        await client.stop()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(run_task, timeout=5)
        if not run_task.done():
            run_task.cancel()
        await bridge.stop()
        await server.stop()
        db.close()


async def _ret(url: str) -> str:
    return url
