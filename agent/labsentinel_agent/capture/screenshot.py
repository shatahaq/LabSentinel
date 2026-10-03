"""Screenshot capture + JPEG compression (mss + Pillow).

Implements the ScreenshotCapturer port. Captures the primary monitor, optionally
downscales to a max width, and encodes to JPEG at a target quality to keep
evidence small on the wire. Returns None (never raises) on any failure so a
capture problem can never break enforcement.
"""

from __future__ import annotations

import io
import logging

log = logging.getLogger("labsentinel.agent.capture")


class MssScreenshotCapturer:
    """Primary-monitor capturer. Lazily initializes mss per call (thread-safe)."""

    def __init__(self, monitor_index: int = 1) -> None:
        # mss monitor index 1 == primary; 0 == virtual "all monitors" bounding box.
        self._monitor_index = monitor_index

    def capture_jpeg(self, *, max_width: int = 1280, quality: int = 60) -> bytes | None:
        try:
            import mss
            from PIL import Image
        except ImportError:
            log.warning("mss/Pillow not available; cannot capture screenshot")
            return None

        # mss renamed the factory to MSS in 10.x; fall back for older versions.
        mss_factory = getattr(mss, "MSS", None) or mss.mss
        try:
            with mss_factory() as sct:
                monitors = sct.monitors
                idx = self._monitor_index if self._monitor_index < len(monitors) else 0
                shot = sct.grab(monitors[idx])
                img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

            if img.width > max_width:
                ratio = max_width / img.width
                img = img.resize((max_width, max(1, int(img.height * ratio))))

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            return buf.getvalue()
        except Exception:
            log.warning("screenshot capture failed", exc_info=True)
            return None
