"""Real-Windows platform tests (process enumeration + control).

These run against the live machine, so they are the closest automated proof we
can get for the safety guarantees short of the manual checklist. Skipped off
Windows.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.windows,
    pytest.mark.skipif(sys.platform != "win32", reason="requires Windows"),
]

from labsentinel_agent.enforce.safety import SafetyPolicy, SafetyVerdict  # noqa: E402
from labsentinel_agent.platform.win_process import (  # noqa: E402
    WinProcessController,
    collect_processes,
    process_info_for,
)

# Critical processes that MUST exist and MUST never be evaluable.
CRITICAL = {"csrss.exe", "winlogon.exe", "services.exe", "lsass.exe", "wininit.exe"}


def test_enumeration_returns_processes() -> None:
    procs = collect_processes()
    assert len(procs) > 10
    names = {p.name for p in procs}
    # The shell is virtually always running.
    assert any(n in names for n in ("explorer.exe", "svchost.exe"))


def test_no_critical_system_process_is_ever_evaluable() -> None:
    """The headline safety guarantee, verified against this live machine."""
    policy = SafetyPolicy(own_pids=frozenset({os.getpid()}))
    procs = collect_processes()
    seen_critical = set()
    for p in procs:
        decision = policy.evaluate(p)
        if p.name in CRITICAL:
            seen_critical.add(p.name)
            assert decision.verdict is SafetyVerdict.PROTECTED, (
                f"CRITICAL: {p.name} (pid={p.pid}) was marked {decision.verdict}!"
            )
        # Any system-owned process must be protected, whatever its name.
        if p.is_system_owned():
            assert decision.verdict is SafetyVerdict.PROTECTED, (
                f"system-owned {p.name} (pid={p.pid}) was marked evaluable!"
            )
    # Make sure we actually exercised some critical processes.
    assert seen_critical, "expected to observe at least one critical system process"


def test_agent_own_pid_protected() -> None:
    me = process_info_for(os.getpid())
    assert me is not None
    policy = SafetyPolicy(own_pids=frozenset({os.getpid()}))
    assert policy.evaluate(me).verdict is SafetyVerdict.PROTECTED


@pytest.mark.asyncio
async def test_controller_force_kill_real_child() -> None:
    # A harmless, windowless child we are allowed to kill.
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        ctrl = WinProcessController()
        # Give it a moment to appear.
        for _ in range(50):
            if ctrl.is_running(child.pid):
                break
            time.sleep(0.02)
        assert ctrl.is_running(child.pid) is True

        killed = await ctrl.force_kill(child.pid)
        assert killed is True
        assert ctrl.is_running(child.pid) is False
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)


@pytest.mark.asyncio
async def test_graceful_close_windowless_falls_through_to_not_closed() -> None:
    # A windowless process gets no WM_CLOSE, so graceful_close should report
    # False within the timeout (engine would then force-kill).
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        ctrl = WinProcessController()
        time.sleep(0.2)
        closed = await ctrl.graceful_close(child.pid, timeout_s=0.5)
        assert closed is False
        assert ctrl.is_running(child.pid) is True
    finally:
        child.kill()
        child.wait(timeout=5)


def test_spawn_constants_match_pywin32() -> None:
    """The Win32 values spawn_in_session spells out agree with pywin32's where it has them."""
    import ntsecuritycon
    import win32con
    import win32process
    import win32security

    from labsentinel_agent.desktop import spawn

    assert spawn.TOKEN_ASSIGN_PRIMARY == ntsecuritycon.TOKEN_ASSIGN_PRIMARY
    assert spawn.TOKEN_DUPLICATE == ntsecuritycon.TOKEN_DUPLICATE
    assert spawn.TOKEN_QUERY == ntsecuritycon.TOKEN_QUERY
    assert spawn.TOKEN_ADJUST_DEFAULT == ntsecuritycon.TOKEN_ADJUST_DEFAULT
    # pywin32 keeps the pre-Windows 2000 value, without TOKEN_ADJUST_SESSIONID;
    # today's winnt.h includes it (0xF01FF).
    assert spawn.TOKEN_ALL_ACCESS == (ntsecuritycon.TOKEN_ALL_ACCESS
                                      | spawn.TOKEN_ADJUST_SESSIONID)
    assert win32security.TokenSessionId == spawn.TOKEN_SESSION_ID_CLASS
    assert win32security.SecurityImpersonation == spawn.SECURITY_IMPERSONATION
    assert win32security.TokenPrimary == spawn.TOKEN_PRIMARY
    assert spawn.CREATE_NO_WINDOW == win32process.CREATE_NO_WINDOW
    assert spawn.CREATE_NEW_PROCESS_GROUP == win32process.CREATE_NEW_PROCESS_GROUP
    assert spawn.STARTF_USESHOWWINDOW == win32con.STARTF_USESHOWWINDOW
    assert spawn.SW_HIDE == win32con.SW_HIDE


def test_own_token_can_be_opened_with_the_rights_spawn_asks_for() -> None:
    """Opening our own token with these rights must not fail (any user may)."""
    import win32api
    import win32security

    from labsentinel_agent.desktop.spawn import (
        OWN_TOKEN_RIGHTS,
        SECURITY_IMPERSONATION,
        TOKEN_ALL_ACCESS,
        TOKEN_PRIMARY,
    )

    own = win32security.OpenProcessToken(win32api.GetCurrentProcess(), OWN_TOKEN_RIGHTS)
    dup = win32security.DuplicateTokenEx(own, SECURITY_IMPERSONATION, TOKEN_ALL_ACCESS,
                                         TOKEN_PRIMARY, None)
    assert dup


def test_elevation_of_this_process_is_read() -> None:
    """is_elevated agrees with Windows' own answer for the test process."""
    import ctypes

    from labsentinel_agent.platform.win_process import is_elevated

    admin = bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    assert is_elevated(os.getpid()) is admin


def test_elevation_of_a_missing_process_is_false() -> None:
    from labsentinel_agent.platform.win_process import is_elevated

    assert is_elevated(0x7FFFFFF0) is False
