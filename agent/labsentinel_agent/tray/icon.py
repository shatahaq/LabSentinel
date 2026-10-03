"""Tray icon rendering and the indicator port the helper drives.

``TrayIndicator`` is the small surface the helper logic needs. ``NullIndicator``
records calls (dev/tests, headless). ``PystrayIndicator`` renders a real tray
icon on Windows.

Accessibility: state is never conveyed by colour alone — the tooltip and the
menu's first entry always spell the state out in words, and each state uses a
distinct glyph as well as a distinct colour.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from labsentinel_shared.enums import AgentState

log = logging.getLogger("labsentinel.agent.tray.icon")

# state -> (Indonesian label, colour, glyph)
_PRESENTATION: dict[str, tuple[str, tuple[int, int, int], str]] = {
    "enforcing": ("Pemantauan aktif", (22, 128, 57), "●"),
    "dry_run": ("Mode uji coba (hanya mencatat)", (180, 120, 10), "◐"),
    "paused": ("Pemantauan dijeda", (190, 140, 10), "||"),
    # The glyphs are drawn as icon artwork, not parsed as text.
    "disconnected": ("Terputus dari pengawas", (150, 40, 40), "×"),  # noqa: RUF001
}


def presentation(state: AgentState, *, connected: bool) -> tuple[str, tuple[int, int, int], str]:
    """Label, colour and glyph for a status. Disconnection outranks the state."""
    if not connected:
        return _PRESENTATION["disconnected"]
    return _PRESENTATION.get(str(state), _PRESENTATION["enforcing"])


def tooltip(state: AgentState, *, connected: bool) -> str:
    label, _, _ = presentation(state, connected=connected)
    return f"Lab Sentinel — {label}"


class TrayIndicator(Protocol):
    """What the helper needs from an on-screen indicator."""

    def set_status(self, state: AgentState, *, connected: bool) -> None: ...

    def show_warning(self, app: str, seconds: int, reason: str) -> None: ...

    def show_message(self, title: str, message: str) -> None: ...

    def run(self) -> None:
        """Block, pumping the UI event loop (if any)."""
        ...

    def stop(self) -> None: ...


@dataclass
class NullIndicator:
    """Headless indicator that just records what it was asked to show."""

    statuses: list[tuple[AgentState, bool]] = field(default_factory=list)
    warnings: list[tuple[str, int, str]] = field(default_factory=list)
    messages: list[tuple[str, str]] = field(default_factory=list)
    running: bool = False

    def set_status(self, state: AgentState, *, connected: bool) -> None:
        self.statuses.append((state, connected))

    def show_warning(self, app: str, seconds: int, reason: str) -> None:
        self.warnings.append((app, seconds, reason))

    def show_message(self, title: str, message: str) -> None:
        self.messages.append((title, message))

    def run(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    @property
    def last_tooltip(self) -> str:
        if not self.statuses:
            return ""
        state, connected = self.statuses[-1]
        return tooltip(state, connected=connected)


def render_icon_image(state: AgentState, *, connected: bool, size: int = 64):  # type: ignore[no-untyped-def]
    """Draw the tray icon: a coloured disc with a state glyph. Returns a PIL image."""
    from PIL import Image, ImageDraw

    _, colour, glyph = presentation(state, connected=connected)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = max(2, size // 16)
    draw.ellipse((pad, pad, size - pad, size - pad), fill=(*colour, 255))

    # Centre the glyph using its measured bounding box.
    try:
        box = draw.textbbox((0, 0), glyph)
        x = (size - (box[2] - box[0])) / 2 - box[0]
        y = (size - (box[3] - box[1])) / 2 - box[1]
        draw.text((x, y), glyph, fill=(255, 255, 255, 255))
    except Exception:  # pragma: no cover - font edge cases
        log.debug("could not draw tray glyph", exc_info=True)
    return img


class PystrayIndicator:
    """Real Windows tray icon. Participants can see, but never change, status."""

    def __init__(self, *, app_name: str = "Lab Sentinel") -> None:
        self._app_name = app_name
        self._state = AgentState.ENFORCING
        self._connected = False
        self._icon: object | None = None

    def _build(self) -> object:
        import pystray

        return pystray.Icon(
            "labsentinel",
            icon=render_icon_image(self._state, connected=self._connected),
            title=tooltip(self._state, connected=self._connected),
            menu=self._build_menu(),
        )

    def set_status(self, state: AgentState, *, connected: bool) -> None:
        self._state = state
        self._connected = connected
        icon = self._icon
        if icon is None:
            return
        try:
            icon.icon = render_icon_image(state, connected=connected)  # type: ignore[attr-defined]
            icon.title = tooltip(state, connected=connected)  # type: ignore[attr-defined]
            icon.menu = self._build_menu()  # type: ignore[attr-defined]
            icon.update_menu()  # type: ignore[attr-defined]
        except Exception:
            log.warning("could not update tray icon", exc_info=True)

    def _build_menu(self) -> object:
        import pystray

        label, _, _ = presentation(self._state, connected=self._connected)
        # Status first, in words, and read-only: the participant can see what is
        # happening but has no control over it.
        return pystray.Menu(
            pystray.MenuItem(label, None, enabled=False),
            pystray.MenuItem("Pemantauan dikendalikan oleh pengawas", None, enabled=False),
        )

    def show_warning(self, app: str, seconds: int, reason: str) -> None:
        self.show_message(
            "Aplikasi tidak diizinkan",
            f"'{app}' akan ditutup dalam {seconds} detik.\nSilakan simpan pekerjaan Anda.",
        )

    def show_message(self, title: str, message: str) -> None:
        icon = self._icon
        if icon is None:
            return
        try:
            # Balloon notification: visible but does not steal keyboard focus.
            icon.notify(message, title)  # type: ignore[attr-defined]
        except Exception:
            log.warning("could not show tray notification", exc_info=True)

    def run(self) -> None:
        self._icon = self._build()
        self._icon.run()  # type: ignore[attr-defined]

    def stop(self) -> None:
        icon = self._icon
        if icon is not None:
            with_stop = getattr(icon, "stop", None)
            if callable(with_stop):
                try:
                    with_stop()
                except Exception:
                    log.debug("tray icon stop failed", exc_info=True)
