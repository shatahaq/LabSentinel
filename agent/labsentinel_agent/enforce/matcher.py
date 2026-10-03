"""Whitelist matching with layered identity verification.

Runs only on processes the safety gate marked EVALUABLE. A process is allowed
only if its name is whitelisted AND it passes any identity checks pinned to that
rule (expected path fragment and/or SHA-256). This defeats the classic evasion
of renaming ``game.exe`` to ``chrome.exe``: the name matches but the hash/path
does not, so it is flagged as impersonation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from labsentinel_agent.enforce.identity import HashCache, ProcessInfo
from labsentinel_shared.rules import DEFAULT_WHITELIST, AppRule, RuleSet


class MatchVerdict(StrEnum):
    ALLOWED = "allowed"
    NOT_WHITELISTED = "not_whitelisted"
    IMPERSONATION = "impersonation"  # whitelisted name but failed identity check


@dataclass(frozen=True)
class MatchResult:
    verdict: MatchVerdict
    reason: str

    @property
    def is_violation(self) -> bool:
        return self.verdict is not MatchVerdict.ALLOWED


class WhitelistMatcher:
    def __init__(self, hash_cache: HashCache | None = None) -> None:
        self._hashes = hash_cache or HashCache()

    def check(self, proc: ProcessInfo, ruleset: RuleSet) -> MatchResult:
        name = (proc.name or "").strip().lower()
        allowed_names = ruleset.effective_whitelist_names()
        if name not in allowed_names:
            return MatchResult(MatchVerdict.NOT_WHITELISTED, f"'{name}' not in whitelist")

        # Find a rule with pinned identity for this name, if any.
        rule = self._find_rule(name, ruleset)
        if rule is None or (rule.expected_sha256 is None and rule.expected_path_contains is None):
            # Default-whitelisted (e.g. browsers) or no pinning -> allow by name.
            return MatchResult(MatchVerdict.ALLOWED, f"'{name}' whitelisted by name")

        # Path check.
        if rule.expected_path_contains is not None:
            path = (proc.exe_path or "").replace("\\", "/").lower()
            if rule.expected_path_contains.replace("\\", "/").lower() not in path:
                return MatchResult(
                    MatchVerdict.IMPERSONATION,
                    f"'{name}' path mismatch (expected to contain "
                    f"'{rule.expected_path_contains}', got '{proc.exe_path}')",
                )

        # Hash check.
        if rule.expected_sha256 is not None:
            if not proc.exe_path:
                return MatchResult(
                    MatchVerdict.IMPERSONATION, f"'{name}' has no path to verify hash"
                )
            actual = self._hashes.sha256(proc.exe_path)
            if actual is None:
                return MatchResult(
                    MatchVerdict.IMPERSONATION, f"'{name}' could not be hashed for verification"
                )
            if actual != rule.expected_sha256:
                return MatchResult(
                    MatchVerdict.IMPERSONATION,
                    f"'{name}' SHA-256 mismatch (expected {rule.expected_sha256[:12]}…, "
                    f"got {actual[:12]}…)",
                )

        return MatchResult(MatchVerdict.ALLOWED, f"'{name}' verified")

    @staticmethod
    def _find_rule(name: str, ruleset: RuleSet) -> AppRule | None:
        for rule in ruleset.app_whitelist:
            if rule.process_name == name:
                return rule
        # Default-whitelisted names have no explicit rule object.
        if name in DEFAULT_WHITELIST:
            return None
        return None
