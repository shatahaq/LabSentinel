"""AgentCore <-> browser policy wiring: blocking follows pause/resume/dry-run."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from labsentinel_agent.core import AgentCore, RuntimeState, _Detector
from labsentinel_agent.enforce.engine import EnforcementEngine
from labsentinel_agent.enforce.matcher import WhitelistMatcher
from labsentinel_agent.enforce.safety import SafetyPolicy
from labsentinel_agent.policy.browser_policy import BrowserPolicyManager
from labsentinel_agent.policy.registry import FakeRegistry, Hive, ValueType
from labsentinel_agent.rule_cache import RuleCache
from labsentinel_shared.enums import AgentState, CommandType
from labsentinel_shared.protocol import CommandPayload
from labsentinel_shared.rules import RuleSet, SiteRule

CHROME_BL = r"SOFTWARE\Policies\Google\Chrome\URLBlocklist"

BLOCKED = (
    SiteRule(pattern="*://*.facebook.com/*", category="media sosial"),
    SiteRule(pattern="chat.openai.com", category="AI chatbot"),
)


class _NoController:
    async def graceful_close(self, pid: int, timeout_s: float) -> bool:
        return True

    async def force_kill(self, pid: int) -> bool:
        return True

    def is_running(self, pid: int) -> bool:
        return False


class _NoWarner:
    def warn_closing(self, process_name: str, seconds: int, reason: str) -> None: ...
    def notify(self, title: str, message: str) -> None: ...


def _blocklist(reg: FakeRegistry) -> list[str]:
    vals = reg.read_values(Hive.HKLM, CHROME_BL)
    return [v for _, (v, _t) in sorted(vals.items(), key=lambda kv: int(kv[0]))]


def _build(tmp_path: Path, *, dry_run: bool = False):
    reg = FakeRegistry()
    policy = BrowserPolicyManager(reg, backup_path=tmp_path / "policy.json")
    runtime = RuntimeState(state=AgentState.ENFORCING)

    async def emit(rec: object) -> None:
        return None

    async def no_sleep(_: float) -> None:
        return None

    engine = EnforcementEngine(
        safety=SafetyPolicy(), matcher=WhitelistMatcher(), controller=_NoController(),
        warner=_NoWarner(), capturer=None, emit=emit,  # type: ignore[arg-type]
        state_provider=lambda: runtime.state, ruleset_provider=runtime.effective_ruleset,
        sleep=no_sleep,
    )
    core = AgentCore(
        runtime=runtime, cache=RuleCache(tmp_path / "rules.json"), engine=engine,
        detector=_Detector(watch_new_processes=lambda cb: asyncio.sleep(0),
                           list_processes=lambda: [], stop=lambda: None),
        lock_fn=lambda: True, notify_fn=lambda t, m: None, policy=policy,
    )
    ruleset = RuleSet(revision=1, dry_run=dry_run, site_blocklist=BLOCKED)
    return core, runtime, reg, ruleset


@pytest.mark.asyncio
async def test_rules_apply_policy_when_enforcing(tmp_path: Path) -> None:
    core, _, reg, ruleset = _build(tmp_path)
    await core.on_ruleset(ruleset)
    assert _blocklist(reg) == ["*://*.facebook.com/*", "chat.openai.com"]
    assert "IncognitoModeAvailability" in reg.read_values(
        Hive.HKLM, r"SOFTWARE\Policies\Google\Chrome"
    )


@pytest.mark.asyncio
async def test_dry_run_blocks_nothing(tmp_path: Path) -> None:
    """Audit-only must not actually block sites."""
    core, _, reg, ruleset = _build(tmp_path, dry_run=True)
    await core.on_ruleset(ruleset)
    assert not reg.key_exists(Hive.HKLM, CHROME_BL)


@pytest.mark.asyncio
async def test_pause_unblocks_and_resume_reblocks(tmp_path: Path) -> None:
    core, runtime, reg, ruleset = _build(tmp_path)
    await core.on_ruleset(ruleset)
    assert _blocklist(reg)

    await core.on_command(CommandPayload(command=CommandType.PAUSE, issued_by="dosen"))
    assert runtime.state is AgentState.PAUSED
    assert not reg.key_exists(Hive.HKLM, CHROME_BL)  # sites reachable again

    await core.on_command(CommandPayload(command=CommandType.RESUME, issued_by="dosen"))
    assert _blocklist(reg) == ["*://*.facebook.com/*", "chat.openai.com"]


@pytest.mark.asyncio
async def test_pause_restores_institution_policy_only(tmp_path: Path) -> None:
    """Pausing must not wipe policy the institution set itself."""
    core, _, reg, ruleset = _build(tmp_path)
    reg.create_key(Hive.HKLM, CHROME_BL)
    reg.write_value(Hive.HKLM, CHROME_BL, "1", "malware.example", ValueType.SZ)

    await core.on_ruleset(ruleset)
    assert "malware.example" in _blocklist(reg)

    await core.on_command(CommandPayload(command=CommandType.PAUSE, issued_by="dosen"))
    assert _blocklist(reg) == ["malware.example"]  # institution entry survives


@pytest.mark.asyncio
async def test_set_dry_run_command_unblocks(tmp_path: Path) -> None:
    core, _, reg, ruleset = _build(tmp_path)
    await core.on_ruleset(ruleset)
    assert _blocklist(reg)
    await core.on_command(
        CommandPayload(command=CommandType.SET_DRY_RUN, text="true", issued_by="dosen")
    )
    assert not reg.key_exists(Hive.HKLM, CHROME_BL)


@pytest.mark.asyncio
async def test_empty_blocklist_applies_nothing(tmp_path: Path) -> None:
    core, _, reg, _ = _build(tmp_path)
    await core.on_ruleset(RuleSet(revision=1, dry_run=False))
    assert not reg.key_exists(Hive.HKLM, CHROME_BL)


@pytest.mark.asyncio
async def test_core_without_policy_manager_is_fine(tmp_path: Path) -> None:
    """A dev/test agent with no policy manager must still work."""
    core, _, _, ruleset = _build(tmp_path)
    core._policy = None
    await core.on_ruleset(ruleset)
    await core.on_command(CommandPayload(command=CommandType.PAUSE, issued_by="x"))
