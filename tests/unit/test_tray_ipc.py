"""Tray IPC protocol + icon presentation tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from labsentinel_agent.tray.icon import NullIndicator, presentation, render_icon_image, tooltip
from labsentinel_agent.tray.ipc import (
    AliveMsg,
    HelloMsg,
    IpcError,
    NotifyMsg,
    StatusMsg,
    WarnMsg,
    decode,
    encode,
)
from labsentinel_shared.enums import AgentState


def test_roundtrip_each_message() -> None:
    for msg in (
        StatusMsg(state=AgentState.PAUSED, connected=True, dashboard_name="Lab A"),
        WarnMsg(app="game.exe", seconds=5, reason="tidak diizinkan"),
        NotifyMsg(title="Pengumuman", message="Ujian dimulai"),
        HelloMsg(token="t", pid=123, username="student"),
        AliveMsg(),
    ):
        assert decode(encode(msg)) == msg


def test_encode_is_single_line() -> None:
    raw = encode(NotifyMsg(title="a", message="b"))
    assert raw.endswith(b"\n")
    assert raw.count(b"\n") == 1


@pytest.mark.parametrize(
    "raw",
    [
        b"not json\n",
        b"[]\n",
        b"\n",
        b'{"kind": "unknown"}\n',
        b'{"kind": "warn"}\n',  # missing required fields
        b'{"kind": "status", "state": "paused", "surprise": true}\n',  # extra field
    ],
)
def test_malformed_rejected(raw: bytes) -> None:
    with pytest.raises(IpcError):
        decode(raw)


def test_oversize_line_rejected() -> None:
    with pytest.raises(IpcError):
        decode(b"x" * (64 * 1024 + 1))


def test_token_not_in_repr() -> None:
    assert "SECRET" not in repr(HelloMsg(token="SECRET", pid=1))


def test_warn_seconds_bounded() -> None:
    with pytest.raises(ValidationError):
        WarnMsg(app="x", seconds=999)


# --- presentation ----------------------------------------------------------
def test_disconnected_outranks_state() -> None:
    label, _, _ = presentation(AgentState.ENFORCING, connected=False)
    assert "Terputus" in label


def test_each_state_has_distinct_colour_and_glyph() -> None:
    seen: set[tuple[tuple[int, int, int], str]] = set()
    for state in (AgentState.ENFORCING, AgentState.PAUSED, AgentState.DRY_RUN):
        _, colour, glyph = presentation(state, connected=True)
        seen.add((colour, glyph))
    _, colour, glyph = presentation(AgentState.ENFORCING, connected=False)
    seen.add((colour, glyph))
    assert len(seen) == 4  # status is never conveyed by colour alone


def test_tooltip_spells_state_in_words() -> None:
    assert tooltip(AgentState.PAUSED, connected=True) == "Lab Sentinel — Pemantauan dijeda"
    assert "aktif" in tooltip(AgentState.ENFORCING, connected=True)


def test_render_icon_produces_image() -> None:
    img = render_icon_image(AgentState.PAUSED, connected=True, size=32)
    assert img.size == (32, 32)
    assert img.mode == "RGBA"
    # Centre pixel is the status colour (opaque disc).
    assert img.getpixel((16, 16))[3] == 255


def test_null_indicator_records() -> None:
    ind = NullIndicator()
    ind.set_status(AgentState.PAUSED, connected=True)
    ind.show_warning("game.exe", 5, "x")
    ind.show_message("t", "m")
    assert ind.statuses == [(AgentState.PAUSED, True)]
    assert ind.warnings == [("game.exe", 5, "x")]
    assert ind.messages == [("t", "m")]
    assert ind.last_tooltip == "Lab Sentinel — Pemantauan dijeda"
