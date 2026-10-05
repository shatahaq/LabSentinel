"""Enforcement engine: ties safety + matching + actions together.

For each observed process it: (1) checks the safety gate, (2) matches against the
whitelist, and on a violation (3) records evidence and either acts (warn → grace
→ graceful close → force kill) or, in dry-run, only logs what it *would* have
done. Deduplicates so the same process isn't handled twice and the dashboard
isn't spammed.

The engine performs no Windows I/O directly — it drives injected ports — so its
full flow is unit-tested with fakes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from labsentinel_agent.enforce.identity import ProcessInfo
from labsentinel_agent.enforce.matcher import MatchVerdict, WhitelistMatcher
from labsentinel_agent.enforce.ports import (
    ProcessController,
    ScreenshotCapturer,
    WarningPresenter,
)
from labsentinel_agent.enforce.safety import SafetyPolicy
from labsentinel_shared.enums import AgentState, ViolationKind
from labsentinel_shared.rules import RuleSet

log = logging.getLogger("labsentinel.agent.enforce")


@dataclass(frozen=True)
class ViolationRecord:
    """Emitted on every violation (acted or dry-run); the agent core ships it."""

    kind: ViolationKind
    subject: str  # process/app name
    detail: str
    pid: int
    dry_run: bool
    acted: bool  # whether the process was actually closed
    occurred_at: float
    screenshot_jpeg: bytes | None = None


ViolationSink = Callable[[ViolationRecord], Awaitable[None]]
Sleep = Callable[[float], Awaitable[None]]


class EnforcementEngine:
    def __init__(
        self,
        *,
        safety: SafetyPolicy,
        matcher: WhitelistMatcher,
        controller: ProcessController,
        warner: WarningPresenter,
        capturer: ScreenshotCapturer | None,
        emit: ViolationSink,
        state_provider: Callable[[], AgentState],
        ruleset_provider: Callable[[], RuleSet | None],
        dedup_window_s: float = 20.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        kill_timeout_s: float = 3.0,
    ) -> None:
        self._safety = safety
        self._matcher = matcher
        self._controller = controller
        self._warner = warner
        self._capturer = capturer
        self._emit = emit
        self._state = state_provider
        self._ruleset = ruleset_provider
        self._dedup_window = dedup_window_s
        self._clock = clock
        self._sleep = sleep
        self._kill_timeout = kill_timeout_s
        self._recent: dict[int, float] = {}  # pid -> last handled (monotonic)

    async def handle_process(self, proc: ProcessInfo) -> ViolationRecord | None:
        """Evaluate and, if warranted, enforce. Returns the record or None."""
        state = self._state()
        if state is AgentState.PAUSED:
            return None  # paused: enforce nothing, capture nothing

        ruleset = self._ruleset()
        if ruleset is None:
            return None  # no rules yet; stay safe and do nothing

        # Safety gate first — never act on protected/system/uncertain processes.
        safety = self._safety.evaluate(proc)
        if not safety.evaluable:
            log.debug("skip pid=%d name=%s: %s", proc.pid, proc.name, safety.reason)
            return None

        match = self._matcher.check(proc, ruleset)
        if not match.is_violation:
            return None

        if self._is_duplicate(proc.pid):
            return None

        dry_run = not ruleset.acts or state is AgentState.DRY_RUN
        kind = (
            ViolationKind.IMPERSONATION
            if match.verdict is MatchVerdict.IMPERSONATION
            else ViolationKind.APP_BLOCKED
        )
        return await self._handle_violation(proc, match.reason, kind, dry_run, ruleset)

    async def _handle_violation(
        self,
        proc: ProcessInfo,
        reason: str,
        kind: ViolationKind,
        dry_run: bool,
        ruleset: RuleSet,
    ) -> ViolationRecord:
        screenshot = self._capture()
        acted = False

        if dry_run:
            log.info("[dry-run] would close pid=%d name=%s (%s)", proc.pid, proc.name, reason)
        else:
            acted = await self._close_gracefully(proc, ruleset.close_grace_seconds, reason)

        record = ViolationRecord(
            kind=ViolationKind.APP_CLOSED if acted else kind,
            subject=proc.name,
            detail=reason,
            pid=proc.pid,
            dry_run=dry_run,
            acted=acted,
            occurred_at=time.time(),
            screenshot_jpeg=screenshot,
        )
        await self._emit(record)
        return record

    async def _close_gracefully(self, proc: ProcessInfo, grace: int, reason: str) -> bool:
        # 1. Warn the participant transparently (no focus stealing).
        self._warner.warn_closing(proc.name, grace, reason)
        # 2. Give them a short, configurable grace period.
        if grace > 0:
            await self._sleep(float(grace))
        # 3. If they already closed it, we're done.
        if not self._controller.is_running(proc.pid):
            return True
        # 4. Ask it to close cleanly (WM_CLOSE), then hard-kill only if needed.
        closed = await self._controller.graceful_close(proc.pid, self._kill_timeout)
        if closed:
            return True
        log.warning("force-killing pid=%d name=%s (did not close)", proc.pid, proc.name)
        return await self._controller.force_kill(proc.pid)

    def _capture(self) -> bytes | None:
        if self._capturer is None:
            return None
        try:
            return self._capturer.capture_jpeg()
        except Exception:
            log.warning("screenshot capture failed", exc_info=True)
            return None

    def _is_duplicate(self, pid: int) -> bool:
        now = self._clock()
        # Evict stale entries to keep the map small.
        stale = [p for p, t in self._recent.items() if now - t > self._dedup_window]
        for p in stale:
            del self._recent[p]
        if pid in self._recent:
            return True
        self._recent[pid] = now
        return False
