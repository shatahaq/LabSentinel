"""Dashboard-side streaming control: agents only capture when someone watches."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from labsentinel_dashboard.domain.events import EventBus
from labsentinel_dashboard.domain.rules_service import RulesService
from labsentinel_dashboard.domain.service import DashboardService
from labsentinel_dashboard.storage.db import Database
from labsentinel_shared.enums import CommandType, StreamMode
from labsentinel_shared.protocol import CommandPayload
from labsentinel_shared.rules import RuleSet


class StreamSession:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.hostname = agent_id.upper()
        self.username = "s"
        self.agent_version = "0.1"
        self.remote_addr = "10.0.0.1:1"
        self.stream_modes: list[str] = []

    async def send_command(self, cmd: CommandPayload) -> str:
        if cmd.command is CommandType.SET_STREAM:
            self.stream_modes.append(cmd.text)
        return f"{self.agent_id}-{len(self.stream_modes)}"

    async def send_ruleset(self, ruleset: RuleSet) -> None: ...


class Link:
    def __init__(self) -> None:
        self.by_id: dict[str, StreamSession] = {}

    def get(self, agent_id: str) -> StreamSession | None:
        return self.by_id.get(agent_id)

    def sessions(self) -> list[StreamSession]:
        return list(self.by_id.values())


def _build(tmp_path: Path):
    db = Database(tmp_path / "s.sqlite")
    rules = RulesService(db, default=RuleSet(revision=1))
    svc = DashboardService(
        db=db, bus=EventBus(), rules_provider=rules.current,
        screenshot_dir=tmp_path / "shots", command_timeout_s=1.0,
    )
    link = Link()
    svc.attach(link)
    return db, svc, link


async def _connect(svc: DashboardService, link: Link, agent_id: str) -> StreamSession:
    s = StreamSession(agent_id)
    link.by_id[agent_id] = s
    await svc.on_agent_connected(s)
    await asyncio.sleep(0.02)
    return s


@pytest.mark.asyncio
async def test_nobody_watching_means_stream_off(tmp_path: Path) -> None:
    db, svc, link = _build(tmp_path)
    s = await _connect(svc, link, "pc-01")
    # Default: no admin looking at anything -> agent told to stop capturing.
    assert s.stream_modes == [str(StreamMode.OFF)]
    db.close()


@pytest.mark.asyncio
async def test_opening_grid_requests_thumbnails(tmp_path: Path) -> None:
    db, svc, link = _build(tmp_path)
    a = await _connect(svc, link, "pc-01")
    b = await _connect(svc, link, "pc-02")
    await svc.set_viewing(grid_open=True)
    await asyncio.sleep(0.02)
    assert a.stream_modes[-1] == str(StreamMode.GRID)
    assert b.stream_modes[-1] == str(StreamMode.GRID)
    db.close()


@pytest.mark.asyncio
async def test_focused_pc_gets_hires_others_stay_grid(tmp_path: Path) -> None:
    db, svc, link = _build(tmp_path)
    a = await _connect(svc, link, "pc-01")
    b = await _connect(svc, link, "pc-02")
    await svc.set_viewing(grid_open=True, focused_agent="pc-01")
    await asyncio.sleep(0.02)
    assert a.stream_modes[-1] == str(StreamMode.HIRES)
    assert b.stream_modes[-1] == str(StreamMode.GRID)
    db.close()


@pytest.mark.asyncio
async def test_closing_everything_stops_all_streams(tmp_path: Path) -> None:
    db, svc, link = _build(tmp_path)
    a = await _connect(svc, link, "pc-01")
    await svc.set_viewing(grid_open=True)
    await asyncio.sleep(0.02)
    await svc.set_viewing(grid_open=False)
    await asyncio.sleep(0.02)
    assert a.stream_modes[-1] == str(StreamMode.OFF)
    db.close()


@pytest.mark.asyncio
async def test_mode_not_resent_when_unchanged(tmp_path: Path) -> None:
    """Avoid chattering 40 agents with redundant commands."""
    db, svc, link = _build(tmp_path)
    a = await _connect(svc, link, "pc-01")
    await svc.set_viewing(grid_open=True)
    await asyncio.sleep(0.02)
    before = len(a.stream_modes)
    await svc.set_viewing(grid_open=True)
    await svc.set_viewing(grid_open=True)
    await asyncio.sleep(0.02)
    assert len(a.stream_modes) == before
    db.close()


@pytest.mark.asyncio
async def test_late_joiner_matches_current_view(tmp_path: Path) -> None:
    db, svc, link = _build(tmp_path)
    await _connect(svc, link, "pc-01")
    await svc.set_viewing(grid_open=True)
    await asyncio.sleep(0.02)
    late = await _connect(svc, link, "pc-late")
    assert late.stream_modes[-1] == str(StreamMode.GRID)
    db.close()


@pytest.mark.asyncio
async def test_disconnect_discards_stale_frame(tmp_path: Path) -> None:
    """A disconnected PC must not keep showing its last screen."""
    from labsentinel_shared.protocol import ScreenFramePayload

    db, svc, link = _build(tmp_path)
    await _connect(svc, link, "pc-01")
    await svc.on_screen_frame("pc-01", ScreenFramePayload(
        agent_id="pc-01", width=320, height=180, jpeg_b64="abc", captured_at=1.0))
    assert svc.latest_frame("pc-01") is not None
    await svc.on_agent_disconnected("pc-01", clean=False)
    assert svc.latest_frame("pc-01") is None
    db.close()



@pytest.mark.asyncio
async def test_frames_newer_returns_only_updated_pcs(tmp_path: Path) -> None:
    """One request serves the whole grid: only PCs with a newer frame come back."""
    from labsentinel_shared.protocol import ScreenFramePayload

    db, svc, link = _build(tmp_path)
    for pc in ("pc-01", "pc-02"):
        await _connect(svc, link, pc)

    def frame(pc: str, body: str) -> ScreenFramePayload:
        return ScreenFramePayload(agent_id=pc, width=320, height=180,
                                  jpeg_b64=body, captured_at=1.0)

    await svc.on_screen_frame("pc-01", frame("pc-01", "a1"))
    await svc.on_screen_frame("pc-02", frame("pc-02", "b1"))

    # Viewer has nothing yet: both come back, each with its own number.
    got = svc.frames_newer({"pc-01": 0, "pc-02": 0})
    seqs = {aid: seq for aid, seq, _ in got}
    assert set(seqs) == {"pc-01", "pc-02"}

    # Acknowledge those numbers: nothing new.
    assert svc.frames_newer(seqs) == []

    # Only pc-01 gets a new frame -> only pc-01 comes back.
    await svc.on_screen_frame("pc-01", frame("pc-01", "a2"))
    got = svc.frames_newer(seqs)
    assert [aid for aid, _, _ in got] == ["pc-01"]
    assert got[0][2].jpeg_b64 == "a2" and got[0][1] > seqs["pc-01"]
    db.close()


@pytest.mark.asyncio
async def test_frames_newer_ignores_a_disconnected_pc(tmp_path: Path) -> None:
    from labsentinel_shared.protocol import ScreenFramePayload

    db, svc, link = _build(tmp_path)
    await _connect(svc, link, "pc-01")
    await svc.on_screen_frame("pc-01", ScreenFramePayload(
        agent_id="pc-01", width=320, height=180, jpeg_b64="a", captured_at=1.0))
    await svc.on_agent_disconnected("pc-01", clean=False)
    assert svc.frames_newer({"pc-01": 0}) == []
    db.close()
