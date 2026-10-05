"""Tray bridge <-> helper over a real loopback socket.

Proves the participant-facing indicator works end to end: status reaches the
icon, warnings and broadcasts are displayed, the icon goes "disconnected" when
the service stops, bad tokens are refused, and — importantly — a helper cannot
instruct the service to change enforcement.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

from labsentinel_agent.tray.bridge import TrayBridge
from labsentinel_agent.tray.helper import TrayHelper
from labsentinel_agent.tray.icon import NullIndicator
from labsentinel_agent.tray.ipc import HelloMsg, StatusMsg, decode, encode
from labsentinel_shared.enums import AgentState

pytestmark = pytest.mark.integration


async def _wait(pred, timeout: float = 5.0) -> None:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


def last_status(ind: NullIndicator) -> tuple[AgentState, bool] | None:
    """Most recent status shown, or None if the icon has shown nothing yet."""
    return ind.statuses[-1] if ind.statuses else None


async def _bridge(tmp_path: Path) -> TrayBridge:
    bridge = TrayBridge(
        token_file=tmp_path / "tray_token",
        endpoint_file=tmp_path / "tray_endpoint",
        port=0,
        dashboard_name="Lab A",
    )
    await bridge.start()
    return bridge


def _helper(tmp_path: Path, indicator: NullIndicator) -> TrayHelper:
    return TrayHelper(
        indicator=indicator,
        token_file=tmp_path / "tray_token",
        endpoint_file=tmp_path / "tray_endpoint",
        alive_interval_s=0.05,
        retry_base_s=0.05,
        retry_max_s=0.2,
    )


@pytest.mark.asyncio
async def test_status_reaches_the_indicator(tmp_path: Path) -> None:
    bridge = await _bridge(tmp_path)
    bridge.set_status(AgentState.ENFORCING, connected=True)
    ind = NullIndicator()
    helper = _helper(tmp_path, ind)
    task = asyncio.ensure_future(helper.run())
    try:
        # On connect the bridge pushes current status immediately.
        await _wait(lambda: (AgentState.ENFORCING, True) in ind.statuses)
        assert ind.last_tooltip == "Lab Sentinel — Pemantauan aktif"

        # A pause from the dashboard flows through to the icon.
        bridge.set_status(AgentState.PAUSED, connected=True)
        await _wait(lambda: last_status(ind) == (AgentState.PAUSED, True))
        assert ind.last_tooltip == "Lab Sentinel — Pemantauan dijeda"
    finally:
        helper.stop()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=3)
        task.cancel()
        await bridge.stop()


@pytest.mark.asyncio
async def test_warning_and_broadcast_displayed(tmp_path: Path) -> None:
    bridge = await _bridge(tmp_path)
    ind = NullIndicator()
    helper = _helper(tmp_path, ind)
    task = asyncio.ensure_future(helper.run())
    try:
        # The helper shows "disconnected" before it connects; wait for the
        # service's own status, i.e. a live connection, before sending.
        await _wait(lambda: bridge.indicator_visible)
        # WarningPresenter port, as called by the enforcement engine.
        bridge.warn_closing("game.exe", 5, "tidak ada dalam whitelist")
        await _wait(lambda: bool(ind.warnings))
        assert ind.warnings[0] == ("game.exe", 5, "tidak ada dalam whitelist")

        bridge.notify("Pesan dari pengawas", "Ujian dimulai 5 menit lagi")
        await _wait(lambda: bool(ind.messages))
        assert ind.messages[0][1] == "Ujian dimulai 5 menit lagi"
    finally:
        helper.stop()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=3)
        task.cancel()
        await bridge.stop()


@pytest.mark.asyncio
async def test_indicator_shows_disconnected_when_service_stops(tmp_path: Path) -> None:
    bridge = await _bridge(tmp_path)
    bridge.set_status(AgentState.ENFORCING, connected=True)
    ind = NullIndicator()
    helper = _helper(tmp_path, ind)
    task = asyncio.ensure_future(helper.run())
    try:
        await _wait(lambda: last_status(ind) == (AgentState.ENFORCING, True))
        await bridge.stop()
        # Participant must not be left looking at a stale "active" icon.
        await _wait(lambda: last_status(ind) is not None and last_status(ind)[1] is False)
        assert "Terputus" in ind.last_tooltip
    finally:
        helper.stop()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=3)
        task.cancel()


@pytest.mark.asyncio
async def test_indicator_visible_tracks_helper(tmp_path: Path) -> None:
    bridge = await _bridge(tmp_path)
    assert bridge.indicator_visible is False
    ind = NullIndicator()
    helper = _helper(tmp_path, ind)
    task = asyncio.ensure_future(helper.run())
    try:
        await _wait(lambda: bridge.indicator_visible is True)
    finally:
        helper.stop()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=3)
        task.cancel()
    # Helper gone -> service knows the indicator is no longer shown.
    await _wait(lambda: bridge.indicator_visible is False)
    await bridge.stop()


@pytest.mark.asyncio
async def test_wrong_token_is_refused(tmp_path: Path) -> None:
    bridge = await _bridge(tmp_path)
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.bound_port)
        writer.write(encode(HelloMsg(token="WRONG", pid=1, username="attacker")))
        await writer.drain()
        # Connection is closed without ever sending status.
        assert await reader.readline() == b""
        assert bridge.indicator_visible is False
        writer.close()
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_helper_cannot_change_enforcement(tmp_path: Path) -> None:
    """A participant replacing the helper must not be able to pause monitoring."""
    bridge = await _bridge(tmp_path)
    bridge.set_status(AgentState.ENFORCING, connected=True)
    token = (tmp_path / "tray_token").read_text(encoding="utf-8").strip()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.bound_port)
        writer.write(encode(HelloMsg(token=token, pid=1, username="student")))
        await writer.drain()
        first = decode(await reader.readline())
        assert isinstance(first, StatusMsg) and first.state is AgentState.ENFORCING

        # Try to talk the service into "paused" by echoing a status message back.
        writer.write(encode(StatusMsg(state=AgentState.PAUSED, connected=True)))
        await writer.drain()
        await asyncio.sleep(0.1)

        # The service's own status is unchanged; it only broadcasts, never obeys.
        bridge.set_status(AgentState.ENFORCING, connected=True)
        echoed = decode(await reader.readline())
        assert isinstance(echoed, StatusMsg) and echoed.state is AgentState.ENFORCING
        writer.close()
    finally:
        await bridge.stop()


@pytest.mark.asyncio
async def test_helper_waits_for_service_then_connects(tmp_path: Path) -> None:
    """Helper may start before the service; it retries instead of giving up."""
    ind = NullIndicator()
    helper = _helper(tmp_path, ind)
    task = asyncio.ensure_future(helper.run())  # no bridge yet
    try:
        await asyncio.sleep(0.15)
        assert last_status(ind) == (AgentState.ENFORCING, False)  # shown as disconnected
        bridge = await _bridge(tmp_path)
        bridge.set_status(AgentState.DRY_RUN, connected=True)
        await _wait(lambda: last_status(ind) == (AgentState.DRY_RUN, True))
        await bridge.stop()
    finally:
        helper.stop()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=3)
        task.cancel()

