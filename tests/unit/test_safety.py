"""Safety-gate tests — the most important guarantees in the system.

Proves protected/system processes are NEVER evaluable, even when they look like
ordinary foreground apps, and that only interactive-user, windowed, non-protected
processes pass the gate.
"""

from __future__ import annotations

import pytest

from labsentinel_agent.enforce.identity import SYSTEM_ACCOUNTS, ProcessInfo
from labsentinel_agent.enforce.safety import SafetyPolicy, SafetyVerdict
from labsentinel_shared.rules import PROTECTED_PROCESSES


def _user_app(name: str = "game.exe", pid: int = 5000, **kw: object) -> ProcessInfo:
    base: dict[str, object] = {
        "pid": pid,
        "name": name,
        "exe_path": f"C:/Games/{name}",
        "username": "LAB\\student",
        "session_id": 1,
        "has_visible_window": True,
        "window_title": "A Game",
        "is_current_user": True,
    }
    base.update(kw)
    return ProcessInfo(**base)  # type: ignore[arg-type]


def test_ordinary_user_app_is_evaluable() -> None:
    assert SafetyPolicy().evaluate(_user_app()).verdict is SafetyVerdict.EVALUABLE


@pytest.mark.parametrize("name", sorted(PROTECTED_PROCESSES))
def test_protected_processes_never_evaluable(name: str) -> None:
    """Even if a protected process somehow looks like a foreground user app."""
    proc = _user_app(
        name=name,
        username="LAB\\student",
        is_current_user=True,
        has_visible_window=True,
    )
    assert SafetyPolicy().evaluate(proc).verdict is SafetyVerdict.PROTECTED


@pytest.mark.parametrize("account", sorted(a for a in SYSTEM_ACCOUNTS if a))
def test_system_owned_never_evaluable(account: str) -> None:
    proc = _user_app(name="something.exe", username=account, is_current_user=False)
    assert SafetyPolicy().evaluate(proc).verdict is SafetyVerdict.PROTECTED


def test_agents_own_pids_protected() -> None:
    policy = SafetyPolicy(own_pids=frozenset({5000}))
    assert policy.evaluate(_user_app(pid=5000)).verdict is SafetyVerdict.PROTECTED


def test_core_system_pids_protected() -> None:
    for pid in (0, 4):
        assert SafetyPolicy().evaluate(_user_app(pid=pid)).verdict is SafetyVerdict.PROTECTED


def test_non_interactive_user_protected() -> None:
    assert (
        SafetyPolicy().evaluate(_user_app(is_current_user=False)).verdict
        is SafetyVerdict.PROTECTED
    )


def test_no_window_protected() -> None:
    assert (
        SafetyPolicy().evaluate(_user_app(has_visible_window=False)).verdict
        is SafetyVerdict.PROTECTED
    )


def test_empty_name_protected() -> None:
    assert SafetyPolicy().evaluate(_user_app(name="")).verdict is SafetyVerdict.PROTECTED


def test_extra_protected_cannot_remove_hardcoded() -> None:
    # Adding extras must never shrink the protected set.
    policy = SafetyPolicy(extra_protected=frozenset({"notepad.exe"}))
    assert policy.evaluate(_user_app(name="notepad.exe")).verdict is SafetyVerdict.PROTECTED
    assert policy.evaluate(_user_app(name="csrss.exe")).verdict is SafetyVerdict.PROTECTED


def test_name_case_insensitive() -> None:
    assert (
        SafetyPolicy().evaluate(_user_app(name="CSRSS.EXE")).verdict is SafetyVerdict.PROTECTED
    )
