"""Adaptive streaming tests — above all, that nothing is captured when idle."""

from __future__ import annotations

import asyncio

import pytest

from labsentinel_agent.capture.screenshot import MssScreenshotCapturer
from labsentinel_agent.capture.streamer import (
    DEFAULT_PROFILES,
    ScreenStreamer,
    StreamProfile,
    _jpeg_size,
)
from labsentinel_shared.enums import AgentState, StreamMode


class CountingCapturer:
    """Records every capture request so we can prove when none happen."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, int]] = []

    def capture_jpeg(self, *, max_width: int = 1280, quality: int = 60) -> bytes | None:
        self.calls.append((max_width, quality))
        return b"\xff\xd8\xff" + bytes([len(self.calls) % 251]) + b"\xff\xd9"


def _build(capturer, state=AgentState.ENFORCING, **kw):
    sent: list[tuple[int, int, bool]] = []

    async def send(jpeg: bytes, w: int, h: int, hi_res: bool) -> bool:
        sent.append((w, h, hi_res))
        return True

    async def run_capture(grab):
        return grab()

    streamer = ScreenStreamer(
        capturer=capturer,
        send=kw.pop("send", send),
        state_provider=kw.pop("state_provider", lambda: state),
        run_capture=run_capture,
        **kw,
    )
    return streamer, sent


# ------------------------------------------------------------- gating
def test_off_means_no_capture() -> None:
    cap = CountingCapturer()
    s, _ = _build(cap)
    assert s.mode is StreamMode.OFF
    assert s.should_capture() is False


def test_paused_means_no_capture_even_if_asked() -> None:
    """Pausing monitoring must stop the screen being read, whatever the UI said."""
    cap = CountingCapturer()
    s, _ = _build(cap, state=AgentState.PAUSED)
    s.set_mode(StreamMode.HIRES)
    assert s.should_capture() is False


@pytest.mark.asyncio
async def test_run_loop_captures_nothing_while_off() -> None:
    cap = CountingCapturer()
    ticks = {"n": 0}

    async def sleep(_: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 5:
            raise asyncio.CancelledError

    s, sent = _build(cap, sleep=sleep)
    with pytest.raises(asyncio.CancelledError):
        await s.run()
    assert cap.calls == []  # the screen was never touched
    assert sent == []


@pytest.mark.asyncio
async def test_run_loop_captures_nothing_while_paused() -> None:
    cap = CountingCapturer()
    ticks = {"n": 0}

    async def sleep(_: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 5:
            raise asyncio.CancelledError

    s, sent = _build(cap, state=AgentState.PAUSED, sleep=sleep)
    s.set_mode(StreamMode.GRID)
    with pytest.raises(asyncio.CancelledError):
        await s.run()
    assert cap.calls == []
    assert sent == []


# ------------------------------------------------------------- profiles
@pytest.mark.asyncio
async def test_grid_mode_uses_small_low_quality_frames() -> None:
    cap = CountingCapturer()
    ticks = {"n": 0}

    async def sleep(_: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 2:
            raise asyncio.CancelledError

    s, sent = _build(cap, sleep=sleep)
    s.set_mode(StreamMode.GRID)
    with pytest.raises(asyncio.CancelledError):
        await s.run()
    assert cap.calls[0] == (320, 40)
    assert sent and sent[0][2] is False  # not hi-res


@pytest.mark.asyncio
async def test_hires_mode_uses_larger_frames() -> None:
    cap = CountingCapturer()
    ticks = {"n": 0}

    async def sleep(_: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 2:
            raise asyncio.CancelledError

    s, sent = _build(cap, sleep=sleep)
    s.set_mode(StreamMode.HIRES)
    with pytest.raises(asyncio.CancelledError):
        await s.run()
    assert cap.calls[0] == (1280, 70)
    assert sent and sent[0][2] is True


def test_default_profiles_are_adaptive() -> None:
    grid = DEFAULT_PROFILES[StreamMode.GRID]
    hires = DEFAULT_PROFILES[StreamMode.HIRES]
    assert grid.fps < hires.fps
    assert grid.max_width < hires.max_width
    assert grid.quality < hires.quality


@pytest.mark.asyncio
async def test_frame_rate_accounts_for_capture_time() -> None:
    """A slow capture must not stack delays on top of the frame interval."""
    cap = CountingCapturer()
    now = {"t": 0.0}
    waits: list[float] = []

    async def sleep(sec: float) -> None:
        waits.append(sec)
        if len(waits) >= 2:
            raise asyncio.CancelledError

    def clock() -> float:
        now["t"] += 0.4  # each clock read advances time (simulates slow capture)
        return now["t"]

    s, _ = _build(cap, sleep=sleep, clock=clock,
                  profiles={StreamMode.GRID: StreamProfile(fps=1.0, max_width=320, quality=40)})
    s.set_mode(StreamMode.GRID)
    with pytest.raises(asyncio.CancelledError):
        await s.run()
    assert waits[0] == pytest.approx(0.6, abs=0.01)  # 1.0s interval - 0.4s spent


@pytest.mark.asyncio
async def test_slow_link_drops_frames_instead_of_queueing() -> None:
    cap = CountingCapturer()
    ticks = {"n": 0}

    async def sleep(_: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 3:
            raise asyncio.CancelledError

    async def failing_send(jpeg: bytes, w: int, h: int, hi: bool) -> bool:
        return False  # offline / congested

    s, _ = _build(cap, sleep=sleep, send=failing_send)
    s.set_mode(StreamMode.GRID)
    with pytest.raises(asyncio.CancelledError):
        await s.run()
    assert s.frames_dropped > 0 and s.frames_sent == 0


@pytest.mark.asyncio
async def test_capture_failure_does_not_stop_streaming() -> None:
    class Flaky:
        def __init__(self) -> None:
            self.n = 0

        def capture_jpeg(self, *, max_width: int = 1280, quality: int = 60) -> bytes | None:
            self.n += 1
            if self.n == 1:
                raise OSError("display busy")
            return None if self.n == 2 else b"\xff\xd8\xff\xd9"

    ticks = {"n": 0}

    async def sleep(_: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 4:
            raise asyncio.CancelledError

    s, sent = _build(Flaky(), sleep=sleep)
    s.set_mode(StreamMode.GRID)
    with pytest.raises(asyncio.CancelledError):
        await s.run()
    assert len(sent) >= 1  # recovered after the error and the empty frame


@pytest.mark.asyncio
async def test_frame_discarded_if_viewer_leaves_mid_capture() -> None:
    """Don't send a screenshot nobody asked for any more."""
    cap = CountingCapturer()
    ticks = {"n": 0}

    async def sleep(_: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 2:
            raise asyncio.CancelledError

    s, sent = _build(cap, sleep=sleep)

    async def run_capture(grab):
        data = grab()
        s.set_mode(StreamMode.OFF)  # admin closed the view while we captured
        return data

    s._run_capture = run_capture  # type: ignore[assignment]
    s.set_mode(StreamMode.GRID)
    with pytest.raises(asyncio.CancelledError):
        await s.run()
    assert cap.calls  # we did capture
    assert sent == []  # but nothing was sent


# ------------------------------------------------------------- jpeg size
def test_jpeg_size_reads_real_dimensions() -> None:
    jpeg = MssScreenshotCapturer().capture_jpeg(max_width=200, quality=40)
    if jpeg is None:
        pytest.skip("no capturable display")
    width, height = _jpeg_size(jpeg, fallback_width=999)
    assert width == 200
    assert height > 0


def test_jpeg_size_falls_back_on_garbage() -> None:
    assert _jpeg_size(b"\xff\xd8not-a-real-jpeg", fallback_width=320) == (320, 180)
