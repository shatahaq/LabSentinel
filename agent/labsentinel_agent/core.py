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
from typing import Protocol

from labsentinel_agent.enforce.engine import EnforcementEngine, ViolationRecord
from labsentinel_agent.enforce.identity import ProcessInfo
from labsentinel_agent.rule_cache import RuleCache
from labsentinel_agent.transport.client import CommandResult, DashboardClient
from labsentinel_shared.enums import AgentState, CommandType, StreamMode, ViolationKind
from labsentinel_shared.protocol import CommandPayload, ViolationPayload
from labsentinel_shared.rules import RuleSet

log = logging.getLogger("labsentinel.agent.core")


@dataclass
class RuntimeState:
    """Shared, mutable runtime state read by the engine and mutated by core."""

    state: AgentState = AgentState.ENFORCING
    ruleset: RuleSet | None = None
    dry_run_override: bool | None = None
    input_frozen: bool = False  # supervisor froze this PC's keyboard and mouse

    def effective_ruleset(self) -> RuleSet | None:
        if self.ruleset is None:
            return None
        if self.dry_run_override is not None and self.dry_run_override != self.ruleset.dry_run:
            return self.ruleset.model_copy(update={"dry_run": self.dry_run_override})
        return self.ruleset


# Injected side-effect hooks (Windows-backed in production, fakes in tests).
LockFn = Callable[[], bool]
FreezeFn = Callable[[bool], bool]
NotifyFn = Callable[[str, str], None]
Snapshot = Callable[[], list[ProcessInfo]]
# Pushes (state, connected) to the participant's on-screen indicator.
StatusSink = Callable[[AgentState, bool], None]


class PolicyManager(Protocol):
    """Browser site-blocking policy (see policy.browser_policy)."""

    def apply(self, patterns: list[str], *, block_incognito: bool = True) -> None: ...

    def clear(self) -> None: ...

    def recover_if_needed(self) -> bool: ...


class Streamer(Protocol):
    """Adaptive screen streamer (see capture.streamer)."""

    def set_mode(self, mode: StreamMode) -> None: ...


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
        freeze_fn: FreezeFn | None = None,
        notify_fn: NotifyFn,
        status_sink: StatusSink | None = None,
        policy: PolicyManager | None = None,
        streamer: Streamer | None = None,
    ) -> None:
        self._rt = runtime
        self._cache = cache
        self._engine = engine
        self._detector = detector
        self._lock = lock_fn
        self._freeze = freeze_fn
        self._notify = notify_fn
        self._status_sink = status_sink
        self._policy = policy
        self._streamer = streamer
        self._connected = False
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
            self.sync_browser_policy()
            if self._rt.state is AgentState.ENFORCING:
                await self.sweep_running()

    def sync_browser_policy(self) -> None:
        """Make the browser policy match the current state and ruleset.

        Paused, test mode or Bebas means nothing is blocked: the policy is removed and
        the institution's own policy restored. Enforcing re-applies it.
        """
        if self._policy is None:
            return
        ruleset = self._rt.effective_ruleset()
        should_block = (
            self._rt.state is AgentState.ENFORCING
            and ruleset is not None
            and ruleset.acts
            and bool(ruleset.site_blocklist)
        )
        try:
            if should_block and ruleset is not None:
                self._policy.apply(
                    [rule.pattern for rule in ruleset.site_blocklist],
                    block_incognito=ruleset.block_incognito,
                )
            else:
                self._policy.clear()
        except Exception:
            log.warning("could not sync browser policy", exc_info=True)

    async def on_command(self, cmd: CommandPayload) -> CommandResult:
        log.info("command %s from %s", cmd.command, cmd.issued_by or "?")
        if cmd.command is CommandType.PAUSE:
            self._rt.state = AgentState.PAUSED
            # Pausing hands the PC back to the participant, so a manual freeze
            # must not outlive it.
            self._set_frozen(False)
            # The participant must see the paused state immediately.
            self.publish_status()
            # Paused means nothing is blocked: restore the original policy.
            self.sync_browser_policy()
            return CommandResult(resulting_state=self._rt.state, message="paused")
        if cmd.command is CommandType.RESUME:
            self._rt.state = AgentState.ENFORCING
            self.publish_status()
            self.sync_browser_policy()
            await self.sweep_running()
            return CommandResult(resulting_state=self._rt.state, message="resumed")
        if cmd.command is CommandType.SET_DRY_RUN:
            self._rt.dry_run_override = _parse_bool(cmd.text, default=True)
            self.publish_status()
            self.sync_browser_policy()
            return CommandResult(
                resulting_state=self._rt.state,
                message=f"dry_run={self._rt.dry_run_override}",
            )
        if cmd.command is CommandType.LOCK_SCREEN:
            ok = self._lock()
            return CommandResult(resulting_state=self._rt.state, ok=ok, message="lock")
        if cmd.command is CommandType.FREEZE_INPUT:
            on = _parse_bool(cmd.text, default=True)
            ok = self._set_frozen(on)
            self.publish_status()
            return CommandResult(
                resulting_state=self._rt.state, ok=ok, message=f"frozen={on}"
            )
        if cmd.command is CommandType.BROADCAST_MESSAGE:
            self._notify("Pesan dari pengawas", cmd.text)
            return CommandResult(resulting_state=self._rt.state, message="broadcast shown")
        if cmd.command is CommandType.SET_STREAM:
            return self._set_stream(cmd.text)
        return CommandResult(resulting_state=self._rt.state, message="ok")

    def _set_stream(self, raw_mode: str) -> CommandResult:
        if self._streamer is None:
            return CommandResult(resulting_state=self._rt.state, message="no streamer")
        try:
            mode = StreamMode(raw_mode.strip().lower())
        except ValueError:
            log.warning("unknown stream mode %r; turning streaming off", raw_mode)
            mode = StreamMode.OFF
        self._streamer.set_mode(mode)
        return CommandResult(resulting_state=self._rt.state, message=f"stream={mode}")

    def state(self) -> AgentState:
        return self._rt.state

    def input_frozen(self) -> bool:
        return self._rt.input_frozen

    def _set_frozen(self, on: bool) -> bool:
        """Freeze or release the keyboard and mouse; remember the state.

        Returns whether the desktop carried it out. The remembered state only
        follows a success, so a failed freeze is not reported as frozen.
        """
        if self._freeze is None:
            return False
        if on == self._rt.input_frozen:
            return True
        ok = False
        try:
            ok = self._freeze(on)
        except Exception:
            log.warning("freeze_input(%s) failed", on, exc_info=True)
        if ok:
            self._rt.input_frozen = on
        return ok

    def release_input_freeze(self) -> None:
        """Best-effort release, for shutdown so a participant is never stranded."""
        if self._rt.input_frozen:
            self._set_frozen(False)

    # -- participant-visible status ----------------------------------------
    def set_connected(self, connected: bool) -> None:
        """Record dashboard connectivity and refresh the on-screen indicator."""
        if self._connected != connected:
            self._connected = connected
            self.publish_status()

    def publish_status(self) -> None:
        """Push the current status to the participant's tray indicator."""
        if self._status_sink is None:
            return
        try:
            self._status_sink(self._rt.state, self._connected)
        except Exception:
            log.warning("could not update participant indicator", exc_info=True)

    # -- violation emission -------------------------------------------------
    async def emit_violation(self, rec: ViolationRecord) -> bool:
        """Send a violation to the dashboard. Returns False if it was not sent."""
        if self._client is None:
            return False
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
        return sent

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
