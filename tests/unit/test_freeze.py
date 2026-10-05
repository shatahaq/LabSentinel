"""Freeze keyboard & mouse: the command, its state, and the fail-safes."""

from __future__ import annotations

import asyncio

import pytest

from labsentinel_agent.core import AgentCore, RuntimeState, _Detector
from labsentinel_agent.desktop.worker import DesktopWorker
from labsentinel_agent.enforce.engine import EnforcementEngine
from labsentinel_agent.enforce.matcher import WhitelistMatcher
from labsentinel_agent.enforce.safety import SafetyPolicy
from labsentinel_agent.rule_cache import RuleCache
from labsentinel_shared.enums import AgentState, CommandType
from labsentinel_shared.protocol import CommandPayload


class FakeDesktop:
    """Records freeze calls; a freeze may be made to fail."""

    def __init__(self, *, succeeds: bool = True) -> None:
        self.calls: list[bool] = []
        self.frozen = False
        self._succeeds = succeeds

    def freeze(self, on: bool) -> bool:
        self.calls.append(on)
        if not self._succeeds:
            return False
        self.frozen = on
        return True


def _core(tmp_path, desktop: FakeDesktop) -> AgentCore:
    runtime = RuntimeState(state=AgentState.ENFORCING)

    async def emit(_: object) -> None:
        return None

    async def no_sleep(_: float) -> None:
        return None

    engine = EnforcementEngine(
        safety=SafetyPolicy(), matcher=WhitelistMatcher(), controller=_NoCtrl(),
        warner=_NoWarner(), capturer=None, emit=emit,  # type: ignore[arg-type]
        state_provider=lambda: runtime.state, ruleset_provider=runtime.effective_ruleset,
        sleep=no_sleep,
    )
    detector = _Detector(watch_new_processes=lambda cb: asyncio.sleep(0),
                         list_processes=lambda: [], stop=lambda: None)
    return AgentCore(
        runtime=runtime, cache=RuleCache(tmp_path / "rules.json"), engine=engine,
        detector=detector, lock_fn=lambda: True, freeze_fn=desktop.freeze,
        notify_fn=lambda t, m: None,
    )


class _NoCtrl:
    def is_running(self, pid: int) -> bool: return True
    async def graceful_close(self, pid: int, timeout_s: float) -> bool: return True
    async def force_kill(self, pid: int) -> bool: return True


class _NoWarner:
    def warn_closing(self, *a: object) -> None: ...
    def notify(self, *a: object) -> None: ...


async def _cmd(core: AgentCore, on: bool):
    text = "on" if on else "off"
    return await core.on_command(CommandPayload(command=CommandType.FREEZE_INPUT, text=text))


@pytest.mark.asyncio
async def test_freeze_then_unfreeze(tmp_path) -> None:
    d = FakeDesktop()
    core = _core(tmp_path, d)
    assert core.input_frozen() is False

    res = await _cmd(core, True)
    assert res.ok is True and core.input_frozen() is True and d.frozen is True

    res = await _cmd(core, False)
    assert res.ok is True and core.input_frozen() is False and d.frozen is False
    assert d.calls == [True, False]


@pytest.mark.asyncio
async def test_freezing_twice_calls_the_desktop_once(tmp_path) -> None:
    d = FakeDesktop()
    core = _core(tmp_path, d)
    await _cmd(core, True)
    await _cmd(core, True)
    assert d.calls == [True]  # already frozen: no second call


@pytest.mark.asyncio
async def test_a_failed_freeze_is_not_reported_as_frozen(tmp_path) -> None:
    d = FakeDesktop(succeeds=False)
    core = _core(tmp_path, d)
    res = await _cmd(core, True)
    assert res.ok is False and core.input_frozen() is False


@pytest.mark.asyncio
async def test_pausing_releases_a_freeze(tmp_path) -> None:
    """Pause hands the PC back, so a manual freeze must not outlive it."""
    d = FakeDesktop()
    core = _core(tmp_path, d)
    await _cmd(core, True)
    await core.on_command(CommandPayload(command=CommandType.PAUSE))
    assert core.input_frozen() is False and d.frozen is False


@pytest.mark.asyncio
async def test_reassert_reapplies_an_active_freeze(tmp_path) -> None:
    """A fresh desktop worker must take the freeze up again."""
    d = FakeDesktop()
    core = _core(tmp_path, d)
    await _cmd(core, True)
    d.calls.clear()
    d.frozen = False  # the old worker died, releasing BlockInput
    core.reassert_input_freeze()
    assert d.calls == [True] and d.frozen is True and core.input_frozen() is True


@pytest.mark.asyncio
async def test_reassert_does_nothing_when_not_frozen(tmp_path) -> None:
    d = FakeDesktop()
    core = _core(tmp_path, d)
    core.reassert_input_freeze()
    assert d.calls == []


@pytest.mark.asyncio
async def test_reassert_that_fails_stops_claiming_frozen(tmp_path) -> None:
    """If the new worker cannot hold it, do not keep reporting it as frozen."""
    d = FakeDesktop()
    core = _core(tmp_path, d)
    await _cmd(core, True)
    d._succeeds = False
    core.reassert_input_freeze()
    assert core.input_frozen() is False


@pytest.mark.asyncio
async def test_release_on_shutdown_is_best_effort(tmp_path) -> None:
    d = FakeDesktop()
    core = _core(tmp_path, d)
    await _cmd(core, True)
    core.release_input_freeze()
    assert core.input_frozen() is False and d.calls[-1] is False
    core.release_input_freeze()  # idempotent: nothing to release
    assert d.calls == [True, False]


# --------------------------------------------------------------- worker op
class FakeOps:
    def __init__(self) -> None:
        self.frozen: bool | None = None

    def window_index(self): return {}
    def foreground(self): return ("", "")
    def capture_jpeg(self, *, max_width, quality): return None
    def close_windows(self, pid): return True
    def lock(self): return True
    def freeze(self, on: bool) -> bool:
        self.frozen = on
        return True
    def session_id(self): return 1


def _worker(ops: FakeOps) -> DesktopWorker:
    from pathlib import Path
    return DesktopWorker(ops=ops, endpoint_file=Path("x"), token_file=Path("y"))


@pytest.mark.asyncio
async def test_worker_freeze_op_calls_ops(tmp_path) -> None:
    ops = FakeOps()
    worker = _worker(ops)
    on = await worker._answer({"id": 1, "op": "freeze", "args": {"on": True}})
    assert on == {"id": 1, "ok": True, "result": True} and ops.frozen is True
    off = await worker._answer({"id": 2, "op": "freeze", "args": {"on": False}})
    assert off["ok"] is True and ops.frozen is False


@pytest.mark.asyncio
async def test_worker_freeze_defaults_to_on(tmp_path) -> None:
    ops = FakeOps()
    await _worker(ops)._answer({"id": 3, "op": "freeze", "args": {}})
    assert ops.frozen is True
