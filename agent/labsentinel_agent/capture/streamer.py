"""Adaptive screen streaming.

Three levels, set by the dashboard: ``OFF`` (nobody is watching), ``GRID``
(small live thumbnail, ~5 fps, so a supervisor watches the whole lab at once)
and ``HIRES`` (larger and smoother, while an admin has this PC open).

A screen that has not changed is not sent again (only a keep-alive frame every
few seconds), so a lab of quiet screens costs almost nothing on the network.

Privacy and cost both demand that the agent **captures nothing at all** unless
someone is actually looking: when monitoring is paused, or no admin is viewing,
the loop does not touch the screen. That is enforced here and covered by tests
that assert the capturer is never called.

Frames are dropped rather than queued when the link is slow, so a congested
network delays nothing else the agent does.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from labsentinel_agent.enforce.ports import ScreenshotCapturer
from labsentinel_shared.enums import AgentState, StreamMode

log = logging.getLogger("labsentinel.agent.stream")


@dataclass(frozen=True)
class StreamProfile:
    """Capture settings for one streaming level."""

    fps: float
    max_width: int
    quality: int


DEFAULT_PROFILES: dict[StreamMode, StreamProfile] = {
    StreamMode.GRID: StreamProfile(fps=5.0, max_width=400, quality=45),
    StreamMode.HIRES: StreamProfile(fps=10.0, max_width=1280, quality=65),
}

# How long the loop naps while idle before re-checking whether to stream.
IDLE_POLL_S = 0.25

# An unchanged screen is re-sent this often, so the dashboard knows it is live.
UNCHANGED_RESEND_S = 3.0

# Sender: (jpeg, width, height, hi_res) -> delivered?
FrameSender = Callable[[bytes, int, int, bool], Awaitable[bool]]


class ScreenStreamer:
    def __init__(
        self,
        *,
        capturer: ScreenshotCapturer,
        send: FrameSender,
        state_provider: Callable[[], AgentState],
        profiles: dict[StreamMode, StreamProfile] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        run_capture: Callable[[Callable[[], bytes | None]], Awaitable[bytes | None]] | None = None,
    ) -> None:
        self._capturer = capturer
        self._send = send
        self._state = state_provider
        self._profiles = profiles or DEFAULT_PROFILES
        self._sleep = sleep
        self._clock = clock
        # JPEG encoding is CPU-bound; by default it runs in a worker thread so
        # it never blocks heartbeats or enforcement.
        self._run_capture = run_capture or _default_run_capture
        self._mode = StreamMode.OFF
        self._stop = False
        self._last_frame: bytes | None = None  # last delivered frame, this mode
        self._last_sent_at = 0.0
        self.frames_sent = 0
        self.frames_dropped = 0
        self.frames_unchanged = 0

    @property
    def mode(self) -> StreamMode:
        return self._mode

    def set_mode(self, mode: StreamMode) -> None:
        if mode is not self._mode:
            log.info("stream mode: %s -> %s", self._mode, mode)
            self._mode = mode
            self._last_frame = None  # a new view always gets a fresh frame

    def stop(self) -> None:
        self._stop = True

    def should_capture(self) -> bool:
        """True only when an admin is watching and monitoring is not paused."""
        if self._mode is StreamMode.OFF:
            return False
        # Paused means the screen is not watched at all, whatever the dashboard
        # last asked for.
        return self._state() is not AgentState.PAUSED

    async def run(self) -> None:
        while not self._stop:
            if not self.should_capture():
                await self._sleep(IDLE_POLL_S)
                continue
            profile = self._profiles.get(self._mode)
            if profile is None:
                await self._sleep(IDLE_POLL_S)
                continue

            started = self._clock()
            await self._capture_and_send(
                profile, hi_res=self._mode is StreamMode.HIRES, now=started
            )
            # Keep the requested frame rate, accounting for capture time.
            elapsed = self._clock() - started
            await self._sleep(max(0.0, (1.0 / profile.fps) - elapsed))

    async def _capture_and_send(
        self, profile: StreamProfile, *, hi_res: bool, now: float | None = None
    ) -> None:
        def _grab() -> bytes | None:
            return self._capturer.capture_jpeg(
                max_width=profile.max_width, quality=profile.quality
            )

        try:
            jpeg = await self._run_capture(_grab)
        except Exception:
            log.warning("screen capture failed", exc_info=True)
            return
        if not jpeg:
            return

        # Re-check: the admin may have stopped watching mid-capture.
        if not self.should_capture():
            return

        # Same screen as last time: skip it, but keep the view alive.
        if now is None:
            now = self._clock()
        if jpeg == self._last_frame and now - self._last_sent_at < UNCHANGED_RESEND_S:
            self.frames_unchanged += 1
            return

        width, height = _jpeg_size(jpeg, fallback_width=profile.max_width)
        try:
            delivered = await self._send(jpeg, width, height, hi_res)
        except Exception:
            log.debug("frame send failed", exc_info=True)
            delivered = False
        if delivered:
            self.frames_sent += 1
            self._last_frame = jpeg
            self._last_sent_at = now
        else:
            self.frames_dropped += 1


async def _default_run_capture(grab: Callable[[], bytes | None]) -> bytes | None:
    return await asyncio.get_running_loop().run_in_executor(None, grab)


def _jpeg_size(jpeg: bytes, *, fallback_width: int) -> tuple[int, int]:
    """Read width/height from the JPEG SOF marker without decoding the image."""
    i = 2
    n = len(jpeg)
    while i + 9 < n:
        if jpeg[i] != 0xFF:
            i += 1
            continue
        marker = jpeg[i + 1]
        # SOF0..SOF15, excluding DHT(C4), JPG(C8) and DAC(CC).
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(jpeg[i + 5 : i + 7], "big")
            width = int.from_bytes(jpeg[i + 7 : i + 9], "big")
            if width and height:
                return width, height
            break
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        segment = int.from_bytes(jpeg[i + 2 : i + 4], "big")
        if segment <= 0:
            break
        i += 2 + segment
    return fallback_width, max(1, fallback_width * 9 // 16)
