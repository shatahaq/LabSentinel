"""Real screenshot capture test (skips if no capturable display)."""

from __future__ import annotations

import pytest

from labsentinel_agent.capture.screenshot import MssScreenshotCapturer


def test_capture_produces_valid_jpeg() -> None:
    jpeg = MssScreenshotCapturer().capture_jpeg(max_width=640, quality=50)
    if jpeg is None:
        pytest.skip("no capturable display in this environment")
    # JPEG magic bytes.
    assert jpeg[:2] == b"\xff\xd8"
    assert jpeg[-2:] == b"\xff\xd9"
    # Downscaled + compressed should be modest in size.
    assert len(jpeg) < 2_000_000


def test_capture_never_raises_on_bad_monitor() -> None:
    # Out-of-range monitor index falls back to the virtual screen, not a crash.
    jpeg = MssScreenshotCapturer(monitor_index=99).capture_jpeg()
    assert jpeg is None or jpeg[:2] == b"\xff\xd8"
