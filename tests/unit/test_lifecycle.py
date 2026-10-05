"""Anti-tamper building blocks: single instance, run marker, loop watchdog,
tray autostart entry, service runner and service configuration."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from labsentinel_agent import autostart
from labsentinel_agent.lifecycle import InstanceLock, LoopWatchdog, PreviousRun, RunMarker
from labsentinel_agent.platform.acl import restrict_to_admins_cmd
from labsentinel_agent.policy.registry import FakeRegistry, Hive, ValueType
from labsentinel_agent.service import (
    NO_RESTART_EXIT_CODES,
    RESTART_DELAYS_MS,
    ServiceRunner,
    failure_actions,
)


# ------------------------------------------------------------ InstanceLock
def test_only_one_instance_holds_the_lock(tmp_path: Path) -> None:
    a, b = InstanceLock(tmp_path / "agent.lock"), InstanceLock(tmp_path / "agent.lock")
    assert a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_lock_is_released_when_the_holder_is_killed(tmp_path: Path) -> None:
    """A crashed or killed agent must never block the next start."""
    lock_path = tmp_path / "agent.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", (
            "import sys, time; sys.path[:0] = ['agent', 'shared'];"
            "from pathlib import Path;"
            "from labsentinel_agent.lifecycle import InstanceLock;"
            f"lock = InstanceLock(Path({str(lock_path)!r})); assert lock.acquire();"
            "print('held', flush=True); time.sleep(60)"
        )],
        stdout=subprocess.PIPE, text=True, cwd=Path(__file__).resolve().parents[2],
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "held"
        assert not InstanceLock(lock_path).acquire()
        holder.kill()
        holder.wait(timeout=10)
        lock = InstanceLock(lock_path)
        assert lock.acquire()
        lock.release()
    finally:
        holder.kill()


# --------------------------------------------------------------- RunMarker
class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _marker(tmp_path: Path, clock: Clock, boot: float = 0.0) -> RunMarker:
    return RunMarker(tmp_path / "running.json", clock=clock, boot_time=lambda: boot)


def test_first_start_has_nothing_to_report(tmp_path: Path) -> None:
    assert _marker(tmp_path, Clock(1000.0)).begin() is None
    assert (tmp_path / "running.json").exists()


def test_normal_stop_leaves_nothing_to_report(tmp_path: Path) -> None:
    clock = Clock(1000.0)
    m = _marker(tmp_path, clock)
    m.begin()
    m.end()
    assert not (tmp_path / "running.json").exists()
    assert _marker(tmp_path, clock).begin() is None


def test_killed_agent_is_reported_with_last_alive_time(tmp_path: Path) -> None:
    clock = Clock(1000.0)
    first = _marker(tmp_path, clock, boot=500.0)
    first.begin()
    clock.now = 1300.0
    first.touch()
    # ... the process is killed here: no end() ...
    clock.now = 1400.0
    prev = _marker(tmp_path, clock, boot=500.0).begin()
    assert prev is not None
    assert prev.last_alive == 1300.0
    assert prev.started_at == 1000.0
    assert not prev.rebooted_since
    assert prev.reason == ""
    assert "dihentikan paksa saat PC menyala" in prev.describe()


def test_power_loss_is_told_apart_from_a_kill(tmp_path: Path) -> None:
    clock = Clock(1000.0)
    _marker(tmp_path, clock).begin()
    clock.now = 5000.0
    prev = _marker(tmp_path, clock, boot=4000.0).begin()  # PC booted after we died
    assert prev is not None and prev.rebooted_since
    assert "tanpa shutdown normal" in prev.describe()


def test_manual_service_stop_is_remembered(tmp_path: Path) -> None:
    clock = Clock(1000.0)
    m = _marker(tmp_path, clock)
    m.begin()
    m.end(reason="service_stopped")
    prev = _marker(tmp_path, clock).begin()
    assert prev is not None and prev.reason == "service_stopped"
    assert "dihentikan secara manual" in prev.describe()


def test_watchdog_restart_is_not_called_a_kill(tmp_path: Path) -> None:
    clock = Clock(1000.0)
    m = _marker(tmp_path, clock)
    m.begin()
    m.end(reason="watchdog")
    prev = _marker(tmp_path, clock).begin()
    assert prev is not None
    text = prev.describe()
    assert "dimulai ulang otomatis" in text and "paksa" not in text


def test_agent_stall_marks_the_reason_before_exiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import labsentinel_agent.app as app_mod
    from labsentinel_shared.config import AgentConfig

    exits: list[float] = []
    monkeypatch.setattr(app_mod, "exit_for_restart", exits.append)
    monkeypatch.setattr(app_mod, "_registry", FakeRegistry)
    config = AgentConfig(data_dir=tmp_path, token_file=tmp_path / "token")
    agent = app_mod.LabSentinelAgent(config)
    agent.marker.begin()
    agent._on_stall(120.0)
    assert exits == [120.0]
    prev = RunMarker(tmp_path / "running.json").begin()
    assert prev is not None and prev.reason == "watchdog"


def test_unreadable_marker_still_counts_as_abnormal(tmp_path: Path) -> None:
    (tmp_path / "running.json").write_text("{not json", encoding="utf-8")
    prev = _marker(tmp_path, Clock(1000.0)).begin()
    assert prev is not None
    assert json.loads((tmp_path / "running.json").read_text(encoding="utf-8"))["last_alive"]


def test_describe_shows_a_clock_time() -> None:
    at = time.mktime((2026, 10, 5, 9, 12, 40, 0, 0, -1))
    prev = PreviousRun(pid=1, started_at=at - 60, last_alive=at, reason="", rebooted_since=False)
    assert "09:12:40" in prev.describe()


# ------------------------------------------------------------ LoopWatchdog
def test_watchdog_trips_once_after_a_stall() -> None:
    clock = Clock(0.0)
    stalls: list[float] = []
    wd = LoopWatchdog(stall_s=30, clock=clock, on_stall=stalls.append)
    wd.beat()
    clock.now = 29.0
    assert not wd.check()
    clock.now = 31.0
    assert wd.check()
    assert not wd.check()  # one restart request, not a storm
    assert stalls == [31.0]


async def test_watchdog_stays_quiet_while_the_loop_turns() -> None:
    stalls: list[float] = []
    wd = LoopWatchdog(stall_s=0.3, check_every_s=0.05, on_stall=stalls.append)
    wd.start()
    beats = asyncio.ensure_future(wd.run_beats(interval_s=0.05))
    await asyncio.sleep(0.6)
    beats.cancel()
    wd.stop()
    assert stalls == []


async def test_watchdog_catches_a_blocked_loop() -> None:
    tripped = threading.Event()
    wd = LoopWatchdog(stall_s=0.3, check_every_s=0.05, on_stall=lambda _a: tripped.set())
    wd.start()
    beats = asyncio.ensure_future(wd.run_beats(interval_s=0.05))
    await asyncio.sleep(0.1)
    time.sleep(0.8)  # a hung call blocks the whole loop
    beats.cancel()
    wd.stop()
    assert tripped.is_set()


# --------------------------------------------------------------- autostart
def test_tray_autostart_entry_roundtrip() -> None:
    reg = FakeRegistry()
    reg.write_value(Hive.HKLM, autostart.RUN_KEY, "OtherApp", "other.exe", ValueType.SZ)
    assert autostart.current(reg) is None
    autostart.enable(reg, '"C:\\LS\\labsentinel-tray.exe"')
    assert autostart.current(reg) == '"C:\\LS\\labsentinel-tray.exe"'
    autostart.disable(reg)
    assert autostart.current(reg) is None
    # Other programs' entries are never touched.
    assert reg.read_values(Hive.HKLM, autostart.RUN_KEY)["OtherApp"][0] == "other.exe"


def test_tray_autostart_is_restored_if_removed_or_altered() -> None:
    reg = FakeRegistry()
    cmd = '"C:\\LS\\labsentinel-tray.exe"'
    assert autostart.ensure(reg, cmd)  # missing -> restored
    assert not autostart.ensure(reg, cmd)  # intact -> left alone
    reg.write_value(Hive.HKLM, autostart.RUN_KEY, autostart.VALUE_NAME, "evil.exe", ValueType.SZ)
    assert autostart.ensure(reg, cmd)
    assert autostart.current(reg) == cmd


def test_tray_autostart_survives_a_registry_error() -> None:
    reg = FakeRegistry(readonly_paths=frozenset({autostart.RUN_KEY}))
    assert autostart.ensure(reg, "x") is False  # logged, not raised


def test_tray_command_for_installed_and_source_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "executable", "C:/Program Files/LabSentinel/labsentinel-agent.exe")
    frozen = autostart.tray_command(frozen=True, data_dir=Path("C:/ProgramData/LS"))
    assert "labsentinel-tray.exe" in frozen
    assert '--data-dir "' in frozen
    source = autostart.tray_command(frozen=False)
    assert source.endswith("-m labsentinel_agent.tray")


# ---------------------------------------------------------- ServiceRunner
class FakeAgent:
    def __init__(self, *, code: int | None = None) -> None:
        self.code = code
        self.stop_reason = ""
        self.shutdown_called = threading.Event()

    async def run(self) -> int:
        if self.code is not None:
            return self.code
        await asyncio.Event().wait()  # runs until cancelled
        return 0

    async def shutdown(self) -> None:
        self.shutdown_called.set()


def test_service_stop_is_clean_and_carries_the_reason() -> None:
    agent = FakeAgent()
    runner = ServiceRunner(lambda: agent)
    runner.start()
    assert not runner.wait(0.2)  # still running
    assert runner.stop(reason="service_stopped", timeout=5)
    assert agent.shutdown_called.is_set()
    assert agent.stop_reason == "service_stopped"
    assert runner.exit_code == 0


def test_agent_ending_on_its_own_is_visible_to_the_service() -> None:
    agent = FakeAgent(code=2)  # e.g. not paired
    runner = ServiceRunner(lambda: agent)
    runner.start()
    assert runner.wait(5)
    assert runner.exit_code == 2
    assert agent.shutdown_called.is_set()
    assert 2 in NO_RESTART_EXIT_CODES  # the service stops instead of looping


def test_agent_that_fails_to_build_does_not_hang_the_service() -> None:
    def broken() -> FakeAgent:
        raise RuntimeError("bad config")

    runner = ServiceRunner(broken)
    started = time.monotonic()
    runner.start()
    assert runner.wait(5)
    assert runner.exit_code == 1
    assert time.monotonic() - started < 5
    assert runner.stop(timeout=1)  # stopping a finished runner is harmless


# --------------------------------------------------------- configuration
def test_recovery_restarts_three_times_then_keeps_trying() -> None:
    fake = SimpleNamespace(SC_ACTION_RESTART=1)
    cfg = failure_actions(fake)
    assert cfg["Actions"] == [(1, d) for d in RESTART_DELAYS_MS]
    assert RESTART_DELAYS_MS[0] <= 10_000  # back within seconds of a kill
    assert cfg["ResetPeriod"] == 86_400


def test_token_acl_uses_language_independent_sids() -> None:
    cmd = restrict_to_admins_cmd(Path("C:/ProgramData/LabSentinel/agent/token"))
    assert cmd[:3] == ["icacls", str(Path("C:/ProgramData/LabSentinel/agent/token")),
                       "/inheritance:r"]
    assert "*S-1-5-18:F" in cmd and "*S-1-5-32-544:F" in cmd
    assert not any("Users" in part or "Everyone" in part for part in cmd)
