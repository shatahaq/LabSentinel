"""EnforcementEngine flow tests using in-memory fakes (no Windows I/O)."""

from __future__ import annotations

import pytest

from labsentinel_agent.enforce.engine import EnforcementEngine, ViolationRecord
from labsentinel_agent.enforce.identity import ProcessInfo
from labsentinel_agent.enforce.matcher import WhitelistMatcher
from labsentinel_agent.enforce.safety import SafetyPolicy
from labsentinel_shared.enums import AgentState, EnforcementMode, ViolationKind
from labsentinel_shared.rules import AppRule, RuleSet


class FakeController:
    def __init__(self, *, closes_gracefully: bool = True, already_gone: bool = False) -> None:
        self.closes_gracefully = closes_gracefully
        self.already_gone = already_gone
        self.graceful_calls: list[int] = []
        self.kill_calls: list[int] = []

    def is_running(self, pid: int) -> bool:
        return not self.already_gone

    async def graceful_close(self, pid: int, timeout_s: float) -> bool:
        self.graceful_calls.append(pid)
        return self.closes_gracefully

    async def force_kill(self, pid: int) -> bool:
        self.kill_calls.append(pid)
        return True


class FakeWarner:
    def __init__(self) -> None:
        self.warnings: list[tuple[str, int, str]] = []
        self.notes: list[tuple[str, str]] = []

    def warn_closing(self, process_name: str, seconds: int, reason: str) -> None:
        self.warnings.append((process_name, seconds, reason))

    def notify(self, title: str, message: str) -> None:
        self.notes.append((title, message))


class FakeCapturer:
    def capture_jpeg(self, *, max_width: int = 1280, quality: int = 60) -> bytes | None:
        return b"\xff\xd8\xff-fake-jpeg"


def _game() -> ProcessInfo:
    return ProcessInfo(
        pid=5000,
        name="game.exe",
        exe_path="C:/Games/game.exe",
        username="LAB\\student",
        session_id=1,
        has_visible_window=True,
        is_current_user=True,
    )


def _build(
    *,
    ruleset: RuleSet,
    state: AgentState = AgentState.ENFORCING,
    controller: FakeController | None = None,
    capturer: FakeCapturer | None = None,
) -> tuple[EnforcementEngine, list[ViolationRecord], FakeController, FakeWarner]:
    records: list[ViolationRecord] = []
    ctrl = controller or FakeController()
    warner = FakeWarner()

    async def emit(r: ViolationRecord) -> None:
        records.append(r)

    async def no_sleep(_: float) -> None:
        return None

    engine = EnforcementEngine(
        safety=SafetyPolicy(),
        matcher=WhitelistMatcher(),
        controller=ctrl,
        warner=warner,
        capturer=capturer,
        emit=emit,
        state_provider=lambda: state,
        ruleset_provider=lambda: ruleset,
        sleep=no_sleep,
    )
    return engine, records, ctrl, warner


@pytest.mark.asyncio
async def test_enforcing_closes_gracefully() -> None:
    engine, records, ctrl, warner = _build(ruleset=RuleSet(revision=1, dry_run=False))
    rec = await engine.handle_process(_game())
    assert rec is not None
    assert rec.acted is True
    assert rec.kind is ViolationKind.APP_CLOSED
    assert ctrl.graceful_calls == [5000]
    assert ctrl.kill_calls == []  # closed cleanly, no force kill
    assert warner.warnings and warner.warnings[0][0] == "game.exe"
    assert len(records) == 1


@pytest.mark.asyncio
async def test_force_kill_when_graceful_fails() -> None:
    ctrl = FakeController(closes_gracefully=False)
    engine, _, _, _ = _build(ruleset=RuleSet(revision=1, dry_run=False), controller=ctrl)
    rec = await engine.handle_process(_game())
    assert rec is not None and rec.acted is True
    assert ctrl.graceful_calls == [5000]
    assert ctrl.kill_calls == [5000]


@pytest.mark.asyncio
async def test_already_closed_during_grace_no_kill() -> None:
    ctrl = FakeController(already_gone=True)
    engine, _, _, _ = _build(ruleset=RuleSet(revision=1, dry_run=False), controller=ctrl)
    rec = await engine.handle_process(_game())
    assert rec is not None and rec.acted is True
    assert ctrl.graceful_calls == []  # short-circuited: already gone
    assert ctrl.kill_calls == []


@pytest.mark.asyncio
async def test_dry_run_records_but_does_not_act() -> None:
    engine, records, ctrl, warner = _build(ruleset=RuleSet(revision=1, dry_run=True))
    rec = await engine.handle_process(_game())
    assert rec is not None
    assert rec.dry_run is True
    assert rec.acted is False
    assert rec.kind is ViolationKind.APP_BLOCKED
    assert ctrl.graceful_calls == [] and ctrl.kill_calls == []
    assert warner.warnings == []  # no warning popup in dry-run
    assert len(records) == 1


@pytest.mark.asyncio
async def test_paused_does_nothing() -> None:
    engine, records, ctrl, _ = _build(
        ruleset=RuleSet(revision=1, dry_run=False), state=AgentState.PAUSED
    )
    assert await engine.handle_process(_game()) is None
    assert records == [] and ctrl.graceful_calls == []


@pytest.mark.asyncio
async def test_whitelisted_app_ignored() -> None:
    rs = RuleSet(revision=1, dry_run=False, app_whitelist=(AppRule(process_name="code.exe"),))
    engine, records, _, _ = _build(ruleset=rs)
    proc = ProcessInfo(
        pid=1, name="code.exe", exe_path="C:/x/code.exe",
        username="LAB\\student", is_current_user=True, has_visible_window=True,
    )
    assert await engine.handle_process(proc) is None
    assert records == []


@pytest.mark.asyncio
async def test_system_process_never_acted_on() -> None:
    rs = RuleSet(revision=1, dry_run=False)
    engine, records, ctrl, _ = _build(ruleset=rs)
    svchost = ProcessInfo(
        pid=900, name="svchost.exe", username="NT AUTHORITY\\SYSTEM",
        is_current_user=False, has_visible_window=False,
    )
    assert await engine.handle_process(svchost) is None
    assert ctrl.graceful_calls == [] and ctrl.kill_calls == [] and records == []


@pytest.mark.asyncio
async def test_dedup_same_pid() -> None:
    engine, records, _, _ = _build(ruleset=RuleSet(revision=1, dry_run=True))
    await engine.handle_process(_game())
    await engine.handle_process(_game())  # same pid within window
    assert len(records) == 1


@pytest.mark.asyncio
async def test_screenshot_attached_as_evidence() -> None:
    engine, _, _, _ = _build(
        ruleset=RuleSet(revision=1, dry_run=True), capturer=FakeCapturer()
    )
    rec = await engine.handle_process(_game())
    assert rec is not None and rec.screenshot_jpeg == b"\xff\xd8\xff-fake-jpeg"


@pytest.mark.asyncio
async def test_free_mode_only_watches() -> None:
    """Bebas: the app is recorded, but never closed, even with enforcing on."""
    engine, records, ctrl, warner = _build(
        ruleset=RuleSet(revision=1, mode=EnforcementMode.FREE, dry_run=False))
    rec = await engine.handle_process(_game())
    assert rec is not None and rec.dry_run is True and rec.acted is False
    assert ctrl.graceful_calls == [] and ctrl.kill_calls == []
    assert warner.warnings == []
    assert len(records) == 1


@pytest.mark.asyncio
async def test_exam_closes_a_coursework_app() -> None:
    """Ujian: an app allowed for Praktikum is closed like any other."""
    code = ProcessInfo(pid=5001, name="code.exe", exe_path="C:/VSCode/code.exe",
                       username="LAB\\student", session_id=1, has_visible_window=True,
                       is_current_user=True)
    apps = (AppRule(process_name="code.exe"),)
    lab, _, lab_ctrl, _ = _build(ruleset=RuleSet(
        revision=1, mode=EnforcementMode.LAB, dry_run=False, app_whitelist=apps))
    assert await lab.handle_process(code) is None
    exam, _, exam_ctrl, _ = _build(ruleset=RuleSet(
        revision=1, mode=EnforcementMode.EXAM, dry_run=False, app_whitelist=apps))
    rec = await exam.handle_process(code)
    assert rec is not None and rec.acted is True
    assert exam_ctrl.graceful_calls == [5001] and lab_ctrl.graceful_calls == []
