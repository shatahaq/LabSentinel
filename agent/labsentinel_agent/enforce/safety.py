"""The safety gate: decides whether a process may be evaluated/acted on at all.

This is a deny-by-default filter that runs BEFORE any whitelist matching. A
process is only *evaluable* if every safety condition holds. Anything uncertain
(missing owner, unknown session, no window, protected name, the agent itself) is
treated as protected and left completely alone.

Rationale and guarantees are covered by tests in tests/unit/test_safety.py,
including an explicit proof that known system processes are never evaluable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from labsentinel_agent.enforce.identity import ProcessInfo
from labsentinel_shared.rules import PROTECTED_PROCESSES


class SafetyVerdict(StrEnum):
    EVALUABLE = "evaluable"  # safe to check against the whitelist
    PROTECTED = "protected"  # never touch (system/critical/uncertain)


@dataclass(frozen=True)
class SafetyDecision:
    verdict: SafetyVerdict
    reason: str

    @property
    def evaluable(self) -> bool:
        return self.verdict is SafetyVerdict.EVALUABLE


class SafetyPolicy:
    """Deny-by-default gate. The hardcoded protected set cannot be overridden."""

    def __init__(
        self,
        *,
        own_pids: frozenset[int] = frozenset(),
        extra_protected: frozenset[str] = frozenset(),
    ) -> None:
        # extra_protected may ADD to, never remove from, the hardcoded floor.
        self._protected = PROTECTED_PROCESSES | {p.strip().lower() for p in extra_protected}
        self._own_pids = own_pids

    def with_own_pids(self, pids: frozenset[int]) -> SafetyPolicy:
        new = SafetyPolicy(own_pids=self._own_pids | pids)
        new._protected = self._protected
        return new

    def evaluate(self, proc: ProcessInfo) -> SafetyDecision:
        """Return whether ``proc`` may be considered for enforcement."""
        # 1. Never the agent's own processes.
        if proc.pid in self._own_pids:
            return SafetyDecision(SafetyVerdict.PROTECTED, "agent's own process")
        if proc.pid <= 4:  # System Idle (0) and System (4)
            return SafetyDecision(SafetyVerdict.PROTECTED, "core system pid")

        name = (proc.name or "").strip().lower()
        if not name:
            return SafetyDecision(SafetyVerdict.PROTECTED, "unknown process name")

        # 2. Hardcoded protected names (cannot be removed by config).
        if name in self._protected:
            return SafetyDecision(SafetyVerdict.PROTECTED, f"protected process: {name}")

        # 3. Never touch processes owned by a system account.
        if proc.is_system_owned():
            return SafetyDecision(SafetyVerdict.PROTECTED, "system-owned process")

        # 4. Only the logged-in interactive user's processes are in scope.
        if not proc.is_current_user:
            return SafetyDecision(SafetyVerdict.PROTECTED, "not owned by interactive user")

        # 5. Only processes with a visible top-level window (user-facing apps).
        if not proc.has_visible_window:
            return SafetyDecision(SafetyVerdict.PROTECTED, "no visible window")

        return SafetyDecision(SafetyVerdict.EVALUABLE, "user-facing, non-protected")
