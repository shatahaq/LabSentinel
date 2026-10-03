"""Agent orchestrator: wires transport, enforcement, and detection together.

Owns the agent's runtime state (current ruleset + enforcing/paused) and routes:
* rule updates  -> cache + (re)sweep running apps,
* dashboard commands -> pause/resume/lock/broadcast/dry-run, each ACKed,
* engine violations -> ViolationPayload sent to the dashboard with evidence.

Dependencies are injected (client, engine, detector, cache), so the core is
testable with fakes and the composition root (agent app) supplies the real
Windows-backed implementations.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from labsentinel_agent.enforce.engine import EnforcementEngine, ViolationRecord
from labsentinel_agent.enforce.identity import ProcessInfo
from labsentinel_agent.rule_cache import RuleCache
from labsentinel_agent.transport.client import CommandResult, DashboardClient
from labsentinel_shared.enums import AgentState, CommandType, ViolationKind
from labsentinel_shared.protocol import CommandPayload, ViolationPayload
from labsentinel_shared.rules import RuleSet

log = logging.getLogger("labsentinel.agent.core")


@dataclass
class RuntimeState:
    """Shared, mutable runtime state read by the engine and mutated by core."""

    state: AgentState = AgentState.ENFORCING
    ruleset: RuleSet | None = None
    dry_run_override: bool | None = None

    def effective_ruleset(self) -> RuleSet | None:
        if self.ruleset is None:
            return None
        if self.dry_run_override is not None and self.dry_run_override != self.ruleset.dry_run:
            return self.ruleset.model_copy(update={"dry_run": self.dry_run_override})
        return self.ruleset


# Injected side-effect hooks (Windows-backed in production, fakes in tests).
LockFn = Callable[[], bool]
NotifyFn = Callable[[str, str], None]
Snapshot = Callable[[], list[ProcessInfo]]


@dataclass
class _Detector:
    watch_new_processes: Callable[[Callable[[ProcessInfo], Awaitable[None]]], Awaitable[None]]
    list_processes: Snapshot
    stop: Callable[[], None]


class AgentCore:
    def __init__(
        self,
        *,
        runtime: RuntimeState,
        cache: RuleCache,
        engine: EnforcementEngine,
        detector: _Detector,
        lock_fn: LockFn,
        notify_fn: NotifyFn,
    ) -> None:
        self._rt = runtime
        self._cache = cache
        self._engine = engine
        self._detector = detector
        self._lock = lock_fn
        self._notify = notify_fn
        self._client: DashboardClient | None = None
        self._sweep_lock = asyncio.Lock()

    def attach_client(self, client: DashboardClient) -> None:
        self._client = client

    # -- callbacks for the transport client --------------------------------
    async def on_ruleset(self, ruleset: RuleSet) -> None:
        updated = self._cache.update(ruleset)
        if updated:
            self._rt.ruleset = self._cache.current
            log.info("ruleset applied: rev=%d dry_run=%s", ruleset.revision, ruleset.dry_run)
            if self._rt.state is AgentState.ENFORCING:
                await self.sweep_running()

    async def on_command(self, cmd: CommandPayload) -> CommandResult:
        log.info("command %s from %s", cmd.command, cmd.issued_by or "?")
        if cmd.command is CommandType.PAUSE:
            self._rt.state = AgentState.PAUSED
            # (M4) browser policy is cleared on pause by the policy manager.
            return CommandResult(resulting_state=self._rt.state, message="paused")
        if cmd.command is CommandType.RESUME:
            self._rt.state = AgentState.ENFORCING
            await self.sweep_running()
            return CommandResult(resulting_state=self._rt.state, message="resumed")
        if cmd.command is CommandType.SET_DRY_RUN:
            self._rt.dry_run_override = _parse_bool(cmd.text, default=True)
            return CommandResult(
                resulting_state=self._rt.state,
                message=f"dry_run={self._rt.dry_run_override}",
            )
        if cmd.command is CommandType.LOCK_SCREEN:
            ok = self._lock()
            return CommandResult(resulting_state=self._rt.state, ok=ok, message="lock")
        if cmd.command is CommandType.BROADCAST_MESSAGE:
            self._notify("Pesan dari pengawas", cmd.text)
            return CommandResult(resulting_state=self._rt.state, message="broadcast shown")
        # Stream start/stop handled by the capture subsystem (M5); ack ok here.
        return CommandResult(resulting_state=self._rt.state, message="ok")

    def state(self) -> AgentState:
        return self._rt.state

    # -- violation emission -------------------------------------------------
    async def emit_violation(self, rec: ViolationRecord) -> None:
        if self._client is None:
            return
        payload = ViolationPayload(
            agent_id=self._client.agent_id,
            kind=rec.kind,
            subject=rec.subject,
            detail=rec.detail,
            detected_from_title=rec.kind is ViolationKind.SITE_TITLE,
            dry_run=rec.dry_run,
            occurred_at=rec.occurred_at,
            screenshot_b64=(
                base64.b64encode(rec.screenshot_jpeg).decode("ascii")
                if rec.screenshot_jpeg
                else None
            ),
        )
        sent = await self._client.send_event(payload)
        if not sent:
            log.info("violation %s not sent (offline); kept enforcing locally", rec.subject)

    # -- sweeps -------------------------------------------------------------
    async def sweep_running(self) -> None:
        """Evaluate already-running processes (e.g. at startup / on resume)."""
        async with self._sweep_lock:
            for proc in self._detector.list_processes():
                await self._engine.handle_process(proc)

    # -- lifecycle ----------------------------------------------------------
    async def run_detection(self) -> None:
        async def _on_start(proc: ProcessInfo) -> None:
            await self._engine.handle_process(proc)  # result handled via emit()

        await self._detector.watch_new_processes(_on_start)

    def stop(self) -> None:
        self._detector.stop()


def _parse_bool(text: str, *, default: bool) -> bool:
    t = text.strip().lower()
    if t in {"1", "true", "on", "yes"}:
        return True
    if t in {"0", "false", "off", "no"}:
        return False
    return default
