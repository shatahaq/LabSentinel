"""DashboardService + SessionService tests with fake agent sessions."""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from labsentinel_dashboard.domain.events import EventBus, Topic
from labsentinel_dashboard.domain.rules_service import RulesService
from labsentinel_dashboard.domain.service import CommandOutcome, DashboardService
from labsentinel_dashboard.domain.sessions import SessionError, SessionService
from labsentinel_dashboard.storage.db import Database
from labsentinel_shared.enums import (
    AgentState,
    CommandType,
    ConnectionStatus,
    EnforcementMode,
    ViolationKind,
)
from labsentinel_shared.protocol import (
    AckPayload,
    CommandPayload,
    HeartbeatPayload,
    ViolationPayload,
)
from labsentinel_shared.rules import RuleSet

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-body"


class FakeSession:
    """Agent session whose ACK behaviour is scripted: 'ack' | 'fail' | 'silent'."""

    def __init__(self, svc: DashboardService, agent_id: str, behaviour: str = "ack") -> None:
        self.svc = svc
        self.agent_id = agent_id
        self.hostname = agent_id.upper()
        self.username = "student"
        self.agent_version = "0.1.0"
        self.remote_addr = "10.0.0.9:5000"
        self.behaviour = behaviour
        self.state = AgentState.ENFORCING
        self.commands: list[CommandType] = []  # admin commands only
        self.sent_commands: list[CommandPayload] = []
        self.stream_modes: list[str] = []
        self.rulesets: list[RuleSet] = []

    async def send_command(self, cmd: CommandPayload) -> str:
        # Stream level is fire-and-forget plumbing, not an admin action, so it
        # is tracked separately and never ACKed.
        if cmd.command is CommandType.SET_STREAM:
            self.stream_modes.append(cmd.text)
            return f"{self.agent_id}-stream-{len(self.stream_modes)}"
        cid = f"{self.agent_id}-{len(self.commands)}"
        self.commands.append(cmd.command)
        self.sent_commands.append(cmd)
        if self.behaviour != "silent":
            if cmd.command is CommandType.PAUSE:
                self.state = AgentState.PAUSED
            elif cmd.command is CommandType.RESUME:
                self.state = AgentState.ENFORCING
            ack = AckPayload(
                agent_id=self.agent_id,
                ack_id=cid,
                ok=self.behaviour == "ack",
                resulting_state=self.state,
                message="" if self.behaviour == "ack" else "gagal",
            )
            # Deliver the ACK shortly after, like a real round trip.
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(self.svc.on_ack(self.agent_id, ack))
            )
        return cid

    async def send_ruleset(self, ruleset: RuleSet) -> None:
        self.rulesets.append(ruleset)


class FakeLink:
    def __init__(self) -> None:
        self.by_id: dict[str, FakeSession] = {}

    def get(self, agent_id: str) -> FakeSession | None:
        return self.by_id.get(agent_id)

    def sessions(self) -> list[FakeSession]:
        return list(self.by_id.values())


class Clock:
    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _build(tmp_path: Path, clock: Clock | None = None):
    db = Database(tmp_path / "svc.sqlite")
    bus = EventBus()
    rules = RulesService(db, default=RuleSet(revision=0, mode=EnforcementMode.LAB))
    clk = clock or Clock()
    svc = DashboardService(
        db=db,
        bus=bus,
        rules_provider=rules.current,
        screenshot_dir=tmp_path / "shots",
        command_timeout_s=1.0,
        heartbeat_timeout_s=10.0,
        clock=clk,
    )
    link = FakeLink()
    svc.attach(link)
    return db, bus, rules, svc, link, clk


async def _connect(svc: DashboardService, link: FakeLink, agent_id: str,
                   behaviour: str = "ack") -> FakeSession:
    s = FakeSession(svc, agent_id, behaviour)
    link.by_id[agent_id] = s
    await svc.on_agent_connected(s)
    # Let any reconnect re-pause dispatch settle.
    await asyncio.sleep(0.02)
    return s


# --------------------------------------------------------------------------- status
@pytest.mark.asyncio
async def test_connect_marks_online_and_persists(tmp_path: Path) -> None:
    db, _, _, svc, link, _ = _build(tmp_path)
    await _connect(svc, link, "pc-01")
    view = svc.agent("pc-01")
    assert view is not None and view.connection is ConnectionStatus.ONLINE
    assert db.list_agents()[0]["hostname"] == "PC-01"


@pytest.mark.asyncio
async def test_status_reflects_agent_reported_state(tmp_path: Path) -> None:
    _, _, _, svc, link, _ = _build(tmp_path)
    await _connect(svc, link, "pc-01")
    # Agent reports it is paused -> display status is PAUSED, not ONLINE.
    await svc.on_heartbeat("pc-01", HeartbeatPayload(
        agent_id="pc-01", state=AgentState.PAUSED, rule_revision=1))
    assert svc.agent("pc-01").status is ConnectionStatus.PAUSED


@pytest.mark.asyncio
async def test_lost_desktop_access_is_shown_to_the_supervisor(tmp_path: Path) -> None:
    _, _, _, svc, link, _ = _build(tmp_path)
    await _connect(svc, link, "pc-01")
    assert svc.agent("pc-01").to_dict()["desktop_access"] is True
    await svc.on_heartbeat("pc-01", HeartbeatPayload(
        agent_id="pc-01", state=AgentState.ENFORCING, rule_revision=1, desktop_access=False))
    assert svc.agent("pc-01").to_dict()["desktop_access"] is False
    # Agents built before this field existed report nothing: treated as fine.
    await svc.on_heartbeat("pc-01", HeartbeatPayload(
        agent_id="pc-01", state=AgentState.ENFORCING, rule_revision=1))
    assert svc.agent("pc-01").desktop_access is True


@pytest.mark.asyncio
async def test_input_frozen_is_shown_to_the_supervisor(tmp_path: Path) -> None:
    _, _, _, svc, link, _ = _build(tmp_path)
    await _connect(svc, link, "pc-01")
    assert svc.agent("pc-01").to_dict()["input_frozen"] is False
    await svc.on_heartbeat("pc-01", HeartbeatPayload(
        agent_id="pc-01", state=AgentState.ENFORCING, rule_revision=1, input_frozen=True))
    assert svc.agent("pc-01").to_dict()["input_frozen"] is True


@pytest.mark.asyncio
async def test_freeze_input_sends_the_command(tmp_path: Path) -> None:
    _, _, _, svc, link, _ = _build(tmp_path)
    session = await _connect(svc, link, "pc-01")
    report = await svc.freeze_input(["pc-01"], True, actor="dosen")
    assert report.succeeded == ["pc-01"]
    assert session.commands == [CommandType.FREEZE_INPUT]
    sent = session.sent_commands[-1]
    assert sent.command is CommandType.FREEZE_INPUT and sent.text == "on"
    await svc.freeze_input(["pc-01"], False, actor="dosen")
    assert session.sent_commands[-1].text == "off"


@pytest.mark.asyncio
async def test_confirmed_freeze_is_shown_before_the_next_heartbeat(tmp_path: Path) -> None:
    _, _, _, svc, link, _ = _build(tmp_path)
    await _connect(svc, link, "pc-01")
    await _connect(svc, link, "pc-02", behaviour="fail")
    report = await svc.freeze_input(["pc-01", "pc-02"], True, actor="dosen")
    assert report.succeeded == ["pc-01"]
    assert svc.agent("pc-01").input_frozen is True
    assert svc.agent("pc-02").input_frozen is False  # it said no: not shown frozen
    await svc.freeze_input(["pc-01"], False, actor="dosen")
    assert svc.agent("pc-01").input_frozen is False


@pytest.mark.asyncio
async def test_signed_in_participant_replaces_the_service_account(tmp_path: Path) -> None:
    db, _, _, svc, link, _ = _build(tmp_path)
    await _connect(svc, link, "pc-01")  # HELLO from a service: no one signed in yet

    def hb(username: str | None) -> HeartbeatPayload:
        return HeartbeatPayload(agent_id="pc-01", state=AgentState.ENFORCING,
                                rule_revision=1, username=username)

    await svc.on_heartbeat("pc-01", hb("budi"))
    assert svc.agent("pc-01").username == "budi"
    assert db.list_agents()[0]["username"] == "budi"
    await svc.on_heartbeat("pc-01", hb(""))  # signed out
    assert svc.agent("pc-01").username == ""
    await svc.on_heartbeat("pc-01", hb("sari"))
    await svc.on_heartbeat("pc-01", hb(None))  # an older agent says nothing: keep it
    assert svc.agent("pc-01").username == "sari"
    assert db.list_agents()[0]["username"] == "sari"


@pytest.mark.asyncio
async def test_disconnect_clean_vs_dropped(tmp_path: Path) -> None:
    _, _, _, svc, link, _ = _build(tmp_path)
    await _connect(svc, link, "pc-01")
    await _connect(svc, link, "pc-02")
    await svc.on_agent_disconnected("pc-01", clean=True)
    await svc.on_agent_disconnected("pc-02", clean=False)
    assert svc.agent("pc-01").connection is ConnectionStatus.OFFLINE
    assert svc.agent("pc-02").connection is ConnectionStatus.DROPPED


@pytest.mark.asyncio
async def test_heartbeat_timeout_marks_dropped(tmp_path: Path) -> None:
    clk = Clock()
    _, _, _, svc, link, _ = _build(tmp_path, clk)
    await _connect(svc, link, "pc-01")
    clk.t += 100  # far beyond heartbeat_timeout_s
    dropped = svc.check_heartbeats()
    assert dropped == ["pc-01"]
    assert svc.agent("pc-01").connection is ConnectionStatus.DROPPED


# --------------------------------------------------------------------------- commands
@pytest.mark.asyncio
async def test_pause_waits_for_ack_and_reports_real_state(tmp_path: Path) -> None:
    db, _, _, svc, link, _ = _build(tmp_path)
    await _connect(svc, link, "pc-01")
    report = await svc.pause(["pc-01"], actor="dosen")
    assert report.outcomes["pc-01"] is CommandOutcome.ACKED
    assert svc.agent("pc-01").agent_state is AgentState.PAUSED
    cmd = db.get_command("pc-01-0")
    assert cmd["status"] == "acked" and cmd["resulting_state"] == "paused"


@pytest.mark.asyncio
async def test_unreachable_when_not_connected(tmp_path: Path) -> None:
    _, _, _, svc, link, _ = _build(tmp_path)
    await _connect(svc, link, "pc-01")
    await svc.on_agent_disconnected("pc-01", clean=True)
    link.by_id.clear()
    report = await svc.pause(["pc-01"], actor="dosen")
    assert report.outcomes["pc-01"] is CommandOutcome.UNREACHABLE


@pytest.mark.asyncio
async def test_timeout_when_agent_silent(tmp_path: Path) -> None:
    _, _, _, svc, link, _ = _build(tmp_path)
    await _connect(svc, link, "pc-silent", behaviour="silent")
    report = await svc.pause(["pc-silent"], actor="dosen")
    assert report.outcomes["pc-silent"] is CommandOutcome.TIMEOUT


@pytest.mark.asyncio
async def test_failed_when_agent_nacks(tmp_path: Path) -> None:
    _, _, _, svc, link, _ = _build(tmp_path)
    await _connect(svc, link, "pc-bad", behaviour="fail")
    report = await svc.lock_screens(["pc-bad"], actor="dosen")
    assert report.outcomes["pc-bad"] is CommandOutcome.FAILED


@pytest.mark.asyncio
async def test_mass_pause_all(tmp_path: Path) -> None:
    _, _, _, svc, link, _ = _build(tmp_path)
    for i in range(3):
        await _connect(svc, link, f"pc-{i}")
    report = await svc.pause(None, actor="dosen")
    assert len(report.succeeded) == 3
    assert svc.global_paused is True


@pytest.mark.asyncio
async def test_pause_reapplied_on_reconnect(tmp_path: Path) -> None:
    _, _, _, svc, link, _ = _build(tmp_path)
    s = await _connect(svc, link, "pc-01")
    await svc.pause(["pc-01"], actor="dosen")
    assert s.commands == [CommandType.PAUSE]
    # Reconnect: desired-paused is re-applied automatically.
    await svc.on_agent_disconnected("pc-01", clean=False)
    s2 = await _connect(svc, link, "pc-01")
    assert s2.commands == [CommandType.PAUSE]


@pytest.mark.asyncio
async def test_global_pause_covers_latecomers(tmp_path: Path) -> None:
    _, _, _, svc, link, _ = _build(tmp_path)
    await _connect(svc, link, "pc-0")
    await svc.pause(None, actor="dosen")
    # A new PC that joins after "pause all" must be paused too.
    late = await _connect(svc, link, "pc-late")
    assert late.commands == [CommandType.PAUSE]


@pytest.mark.asyncio
async def test_broadcast_requires_text(tmp_path: Path) -> None:
    _, _, _, svc, link, _ = _build(tmp_path)
    await _connect(svc, link, "pc-01")
    with pytest.raises(ValueError):
        await svc.broadcast(["pc-01"], "   ", actor="dosen")


@pytest.mark.asyncio
async def test_resume_single_from_global_pause(tmp_path: Path) -> None:
    _, _, _, svc, link, _ = _build(tmp_path)
    for i in range(3):
        await _connect(svc, link, f"pc-{i}")
    await svc.pause(None, actor="dosen")
    await svc.resume(["pc-1"], actor="dosen")
    assert svc.global_paused is False
    assert svc.is_desired_paused("pc-0") is True
    assert svc.is_desired_paused("pc-1") is False


@pytest.mark.asyncio
async def test_every_command_is_audited(tmp_path: Path) -> None:
    db, _, _, svc, link, _ = _build(tmp_path)
    await _connect(svc, link, "pc-01")
    await svc.pause(["pc-01"], actor="dosen")
    actions = [a.action for a in db.list_audit()]
    assert "command.pause" in actions
    assert "command.pause.hasil" in actions


# --------------------------------------------------------------------------- violations
@pytest.mark.asyncio
async def test_violation_stored_with_screenshot(tmp_path: Path) -> None:
    db, bus, _, svc, link, _ = _build(tmp_path)
    await _connect(svc, link, "pc-01")
    events: list[dict] = []
    bus.subscribe(lambda t, d: events.append(d) if t == Topic.VIOLATION_NEW else None)
    await svc.on_violation("pc-01", ViolationPayload(
        agent_id="pc-01", kind=ViolationKind.APP_BLOCKED, subject="game.exe",
        occurred_at=1_000_000.0, screenshot_b64=base64.b64encode(JPEG).decode()))
    rows = db.list_violations()
    assert len(rows) == 1 and rows[0].subject == "game.exe"
    assert rows[0].screenshot_path is not None
    assert Path(rows[0].screenshot_path).read_bytes() == JPEG
    assert events[0]["has_screenshot"] is True
    assert svc.agent("pc-01").violation_count == 1


@pytest.mark.asyncio
async def test_bad_screenshot_rejected_but_violation_kept(tmp_path: Path) -> None:
    db, _, _, svc, link, _ = _build(tmp_path)
    await _connect(svc, link, "pc-01")
    await svc.on_violation("pc-01", ViolationPayload(
        agent_id="pc-01", kind=ViolationKind.APP_BLOCKED, subject="x.exe",
        occurred_at=1_000_000.0, screenshot_b64="not-valid-base64!!!"))
    rows = db.list_violations()
    assert len(rows) == 1 and rows[0].screenshot_path is None


@pytest.mark.asyncio
async def test_violation_notify_dedup(tmp_path: Path) -> None:
    clk = Clock()
    _, bus, _, svc, link, _ = _build(tmp_path, clk)
    await _connect(svc, link, "pc-01")
    notes: list[bool] = []
    bus.subscribe(lambda t, d: notes.append(d["notify"]) if t == Topic.VIOLATION_NEW else None)

    def viol() -> ViolationPayload:
        return ViolationPayload(agent_id="pc-01", kind=ViolationKind.APP_BLOCKED,
                                subject="game.exe", occurred_at=clk.t)

    await svc.on_violation("pc-01", viol())
    await svc.on_violation("pc-01", viol())  # same subject, immediately -> no toast
    clk.t += 120
    await svc.on_violation("pc-01", viol())  # after dedup window -> toast again
    assert notes == [True, False, True]


# --------------------------------------------------------------------------- retention
@pytest.mark.asyncio
async def test_retention_deletes_files(tmp_path: Path) -> None:
    db, _, _, svc, link, clk = _build(tmp_path, Clock())
    await _connect(svc, link, "pc-01")
    await svc.on_violation("pc-01", ViolationPayload(
        agent_id="pc-01", kind=ViolationKind.APP_BLOCKED, subject="old.exe",
        occurred_at=clk.t, screenshot_b64=base64.b64encode(JPEG).decode()))
    path = Path(db.list_violations()[0].screenshot_path)
    assert path.exists()
    clk.t += 40 * 86400
    removed = svc.purge_expired(retention_days=30)
    assert removed == 1 and not path.exists()


# --------------------------------------------------------------------------- sessions
def _sessions(tmp_path, clk):
    db, bus, rules, svc, link, _ = _build(tmp_path, clk)
    ss = SessionService(db=db, service=svc, rules=rules, bus=bus, clock=clk)
    return db, rules, svc, link, ss


@pytest.mark.asyncio
async def test_start_session_applies_mode_and_resumes(tmp_path: Path) -> None:
    clk = Clock()
    db, rules, svc, link, ss = _sessions(tmp_path, clk)
    s1 = await _connect(svc, link, "pc-01")
    await ss.start_now(name="Ujian A", mode=EnforcementMode.EXAM, actor="dosen")
    assert rules.current().mode is EnforcementMode.EXAM
    assert s1.rulesets and s1.rulesets[-1].mode is EnforcementMode.EXAM
    assert CommandType.RESUME in s1.commands
    assert svc.active_session_id is not None
    # New violations link to the active session.
    await svc.on_violation("pc-01", ViolationPayload(
        agent_id="pc-01", kind=ViolationKind.APP_BLOCKED, subject="g.exe", occurred_at=clk.t))
    assert db.list_violations()[0].session_id == svc.active_session_id


@pytest.mark.asyncio
async def test_cannot_start_two_sessions(tmp_path: Path) -> None:
    clk = Clock()
    _, _, _, _, ss = _sessions(tmp_path, clk)
    await ss.start_now(name="A", mode=EnforcementMode.LAB, actor="dosen")
    with pytest.raises(SessionError):
        await ss.start_now(name="B", mode=EnforcementMode.LAB, actor="dosen")


@pytest.mark.asyncio
async def test_end_session_pauses_all(tmp_path: Path) -> None:
    clk = Clock()
    _, _, svc, link, ss = _sessions(tmp_path, clk)
    s1 = await _connect(svc, link, "pc-01")
    await ss.start_now(name="A", mode=EnforcementMode.LAB, actor="dosen")
    await ss.end_active(actor="dosen")
    assert svc.active_session_id is None
    assert svc.global_paused is True
    assert s1.commands[-1] is CommandType.PAUSE


@pytest.mark.asyncio
async def test_schedule_conflict_rejected(tmp_path: Path) -> None:
    clk = Clock()
    _, _, _, _, ss = _sessions(tmp_path, clk)
    ss.schedule(name="A", mode=EnforcementMode.EXAM,
                start_at=clk.t + 100, end_at=clk.t + 200, actor="dosen")
    with pytest.raises(SessionError):
        ss.schedule(name="B", mode=EnforcementMode.EXAM,
                    start_at=clk.t + 150, end_at=clk.t + 250, actor="dosen")


@pytest.mark.asyncio
async def test_scheduled_session_auto_starts_and_ends(tmp_path: Path) -> None:
    clk = Clock()
    _, _, svc, link, ss = _sessions(tmp_path, clk)
    await _connect(svc, link, "pc-01")
    ss.schedule(name="Ujian", mode=EnforcementMode.EXAM,
                start_at=clk.t + 100, end_at=clk.t + 200, actor="dosen")
    await ss.tick()
    assert svc.active_session_id is None  # not yet
    clk.t += 120
    await ss.tick()
    assert svc.active_session_id is not None  # auto-started
    clk.t += 120
    await ss.tick()
    assert svc.active_session_id is None  # auto-ended
    assert svc.global_paused is True


@pytest.mark.asyncio
async def test_missed_schedule_is_not_started_late(tmp_path: Path) -> None:
    clk = Clock()
    db, _, svc, _, ss = _sessions(tmp_path, clk)
    ss.schedule(name="Lewat", mode=EnforcementMode.EXAM,
                start_at=clk.t + 100, end_at=clk.t + 200, actor="dosen")
    clk.t += 300  # whole window already passed
    await ss.tick()
    assert svc.active_session_id is None
    assert any(a.action == "session.missed" for a in db.list_audit())
