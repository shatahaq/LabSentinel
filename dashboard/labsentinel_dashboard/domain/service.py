"""Dashboard domain service: live PC registry, remote commands, violations.

Implements ``ServerEvents`` for the WebSocket server and is the single place the
UI talks to. Key guarantees:

* **Real state, not button state.** A PC's displayed state comes from its
  heartbeats and command ACKs. A command is only reported successful once the
  agent ACKs it with the resulting state.
* **Every admin action is audited** (who, what, which PCs, outcome) in an
  append-only log; per-PC delivery lives in the ``commands`` table.
* **Desired pause state is authoritative on the dashboard** and persisted, so a
  PC that reconnects (or restarts) is re-paused if the admin had paused it.
* Agents that stop heartbeating without a goodbye are marked ``dropped``.

Runs on the WebSocket asyncio loop. The UI thread calls in via
``asyncio.run_coroutine_threadsafe``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import logging
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from labsentinel_dashboard.domain.events import EventBus, Topic
from labsentinel_dashboard.storage.db import Database
from labsentinel_shared.enums import AgentState, CommandType, ConnectionStatus, StreamMode
from labsentinel_shared.protocol import (
    AckPayload,
    CommandPayload,
    HeartbeatPayload,
    ScreenFramePayload,
    ViolationPayload,
)
from labsentinel_shared.rules import RuleSet

log = logging.getLogger("labsentinel.dashboard.service")

SYSTEM_ACTOR = "sistem"
MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024
_SETTING_PAUSED = "paused_agents"
_SETTING_GLOBAL_PAUSED = "paused_all"


class CommandOutcome(StrEnum):
    ACKED = "acked"
    FAILED = "failed"  # agent ACKed with ok=False
    TIMEOUT = "timeout"  # sent, no ACK in time
    UNREACHABLE = "unreachable"  # PC not connected


class AgentSessionLike(Protocol):
    """What the service needs from a connected agent (the transport session)."""

    agent_id: str
    hostname: str
    username: str
    agent_version: str
    remote_addr: str

    async def send_command(self, cmd: CommandPayload) -> str: ...
    async def send_ruleset(self, ruleset: RuleSet) -> None: ...


class AgentLink(Protocol):
    """What the service needs from the WebSocket server."""

    def get(self, agent_id: str) -> AgentSessionLike | None: ...

    # Sequence (not list) so a concrete server returning its own session type
    # still satisfies this protocol — list is invariant.
    def sessions(self) -> Sequence[AgentSessionLike]: ...


@dataclass
class AgentView:
    """Everything the UI shows about one PC."""

    agent_id: str
    hostname: str
    username: str = ""
    remote_addr: str = ""
    agent_version: str = ""
    group: str = ""
    connection: ConnectionStatus = ConnectionStatus.OFFLINE
    agent_state: AgentState | None = None  # last state reported by the agent itself
    rule_revision: int = -1
    last_heartbeat: float = 0.0
    cpu_percent: float = 0.0
    mem_mb: float = 0.0
    active_window_title: str = ""
    violation_count: int = 0
    # When the last violation happened, so the UI can highlight PCs that are
    # misbehaving *now* rather than any PC that ever misbehaved.
    last_violation_at: float = 0.0
    # False when the PC is monitored but the participant cannot see the tray
    # indicator (helper missing/killed) — a transparency problem to surface.
    indicator_visible: bool = True
    # False when the agent cannot see the desktop (service without its desktop
    # worker): no screen, no titles, apps are not closed. See ADR-0006.
    desktop_access: bool = True

    @property
    def status(self) -> ConnectionStatus:
        """Single status for display: paused wins over online."""
        if self.connection is ConnectionStatus.ONLINE and self.agent_state is AgentState.PAUSED:
            return ConnectionStatus.PAUSED
        return self.connection

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["connection"] = str(self.connection)
        d["agent_state"] = str(self.agent_state) if self.agent_state else None
        d["status"] = str(self.status)
        return d


@dataclass
class CommandReport:
    """Outcome of one admin command across its target PCs."""

    command: CommandType
    actor: str
    outcomes: dict[str, CommandOutcome] = field(default_factory=dict)

    @property
    def succeeded(self) -> list[str]:
        return [a for a, o in self.outcomes.items() if o is CommandOutcome.ACKED]

    @property
    def failed(self) -> list[str]:
        return [a for a, o in self.outcomes.items() if o is not CommandOutcome.ACKED]

    def summary(self) -> str:
        return f"{len(self.succeeded)}/{len(self.outcomes)} PC berhasil"


@dataclass
class _Pending:
    agent_id: str
    command: CommandType
    future: asyncio.Future[AckPayload]


class DashboardService:
    def __init__(
        self,
        *,
        db: Database,
        bus: EventBus,
        rules_provider: Callable[[], RuleSet],
        screenshot_dir: Path,
        command_timeout_s: float = 10.0,
        heartbeat_timeout_s: float = 15.0,
        notify_dedup_s: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._bus = bus
        self._rules = rules_provider
        self._shots = screenshot_dir
        self._cmd_timeout = command_timeout_s
        self._hb_timeout = heartbeat_timeout_s
        self._notify_dedup = notify_dedup_s
        self._clock = clock
        self._link: AgentLink | None = None
        self._views: dict[str, AgentView] = {}
        self._pending: dict[str, _Pending] = {}
        self._last_notified: dict[tuple[str, str], float] = {}
        self._latest_frames: dict[str, ScreenFramePayload] = {}
        self._bg_tasks: set[asyncio.Task[Any]] = set()
        self._grid_open = False
        self._focused_agent: str | None = None
        self._stream_modes: dict[str, StreamMode] = {}
        self._paused: set[str] = set(db.get_setting(_SETTING_PAUSED, default=[]))
        # "Pause all" also covers PCs that join later.
        self._global_paused: bool = bool(db.get_setting(_SETTING_GLOBAL_PAUSED, default=False))
        self.active_session_id: int | None = None
        self._load_known_agents()

    def attach(self, link: AgentLink) -> None:
        self._link = link

    # ------------------------------------------------------------------ queries
    def agents(self) -> list[AgentView]:
        return sorted(self._views.values(), key=lambda v: v.hostname.lower())

    def agent(self, agent_id: str) -> AgentView | None:
        return self._views.get(agent_id)

    def counts(self) -> dict[str, int]:
        out = {str(s): 0 for s in ConnectionStatus}
        for v in self._views.values():
            out[str(v.status)] += 1
        out["total"] = len(self._views)
        return out

    def desired_paused(self) -> frozenset[str]:
        return frozenset(self._paused)

    def is_desired_paused(self, agent_id: str) -> bool:
        return self._global_paused or agent_id in self._paused

    @property
    def global_paused(self) -> bool:
        return self._global_paused

    def latest_frame(self, agent_id: str) -> ScreenFramePayload | None:
        return self._latest_frames.get(agent_id)

    # ------------------------------------------------------------ ServerEvents
    async def on_agent_connected(self, session: AgentSessionLike) -> None:
        view = self._views.get(session.agent_id) or AgentView(
            agent_id=session.agent_id, hostname=session.hostname
        )
        view.hostname = session.hostname
        view.username = session.username
        view.remote_addr = session.remote_addr
        view.agent_version = session.agent_version
        view.connection = ConnectionStatus.ONLINE
        view.last_heartbeat = self._clock()
        self._views[session.agent_id] = view
        self._db.upsert_agent(
            agent_id=session.agent_id,
            hostname=session.hostname,
            username=session.username,
            agent_version=session.agent_version,
            remote_addr=session.remote_addr,
            now=self._clock(),
        )
        self._publish_agent(view)

        # A PC that joins mid-session must match what the admin is watching.
        self._stream_modes.pop(session.agent_id, None)
        self._spawn(self._send_stream_mode(session, self._desired_mode(session.agent_id)))

        # Re-apply the admin's pause decision to a PC that (re)joins.
        if self.is_desired_paused(session.agent_id):
            log.info("re-pausing %s on reconnect (desired state)", session.agent_id)
            self._spawn(
                self._dispatch(
                    [session.agent_id],
                    CommandPayload(command=CommandType.PAUSE, issued_by=SYSTEM_ACTOR),
                    actor=SYSTEM_ACTOR,
                    audit_action="command.pause.reapply",
                )
            )

    async def on_agent_disconnected(self, agent_id: str, *, clean: bool) -> None:
        view = self._views.get(agent_id)
        if view is None:
            return
        view.connection = ConnectionStatus.OFFLINE if clean else ConnectionStatus.DROPPED
        self._fail_pending_for(agent_id)
        self._stream_modes.pop(agent_id, None)
        self._latest_frames.pop(agent_id, None)  # don't show a stale screen
        self._publish_agent(view)

    async def on_heartbeat(self, agent_id: str, hb: HeartbeatPayload) -> None:
        view = self._views.get(agent_id)
        if view is None:
            return
        now = self._clock()
        throttle_db = now - view.last_heartbeat > 30.0
        view.connection = ConnectionStatus.ONLINE
        view.agent_state = hb.state
        view.rule_revision = hb.rule_revision
        view.last_heartbeat = now
        view.cpu_percent = hb.cpu_percent
        view.mem_mb = hb.mem_mb
        view.active_window_title = hb.active_window_title
        view.indicator_visible = hb.indicator_visible
        view.desktop_access = hb.desktop_access
        if throttle_db:
            self._db.touch_agent(agent_id, now)
        self._publish_agent(view)

    async def on_violation(self, agent_id: str, v: ViolationPayload) -> None:
        view = self._views.get(agent_id)
        hostname = view.hostname if view else agent_id
        shot_path = self._save_screenshot(agent_id, v.screenshot_b64, v.occurred_at)
        vid = self._db.add_violation(
            agent_id=agent_id,
            hostname=hostname,
            kind=str(v.kind),
            subject=v.subject,
            detail=v.detail,
            detected_from_title=v.detected_from_title,
            dry_run=v.dry_run,
            occurred_at=v.occurred_at,
            screenshot_path=shot_path,
            session_id=self.active_session_id,
            received_at=self._clock(),
        )
        if view is not None:
            view.violation_count += 1
            view.last_violation_at = max(view.last_violation_at, v.occurred_at)
            self._publish_agent(view)

        key = (agent_id, v.subject.lower())
        now = self._clock()
        notify = now - self._last_notified.get(key, -1e18) >= self._notify_dedup
        if notify:
            self._last_notified[key] = now
        self._bus.publish(
            Topic.VIOLATION_NEW,
            {
                "id": vid,
                "agent_id": agent_id,
                "hostname": hostname,
                "kind": str(v.kind),
                "subject": v.subject,
                "detail": v.detail,
                "detected_from_title": v.detected_from_title,
                "dry_run": v.dry_run,
                "occurred_at": v.occurred_at,
                "has_screenshot": shot_path is not None,
                "notify": notify,  # UI shows a toast only when True (anti-spam)
            },
        )

    async def on_screen_frame(self, agent_id: str, f: ScreenFramePayload) -> None:
        self._latest_frames[agent_id] = f
        self._bus.publish(Topic.FRAME, {"agent_id": agent_id, "hi_res": f.hi_res})

    async def on_ack(self, agent_id: str, ack: AckPayload) -> None:
        pending = self._pending.get(ack.ack_id)
        if pending is None or pending.agent_id != agent_id:
            log.debug("ignoring unknown/late ack %s from %s", ack.ack_id, agent_id)
            return
        view = self._views.get(agent_id)
        if view is not None:
            view.agent_state = ack.resulting_state
            self._publish_agent(view)
        if not pending.future.done():
            pending.future.set_result(ack)

    # ------------------------------------------------------------ admin actions
    async def pause(self, agent_ids: Iterable[str] | None, *, actor: str) -> CommandReport:
        """Pause monitoring. ``None`` = all PCs, including ones that join later."""
        targets = self._resolve(agent_ids)
        if agent_ids is None:
            self._global_paused = True
        self._paused.update(targets)
        self._persist_paused()
        return await self._dispatch(
            targets, CommandPayload(command=CommandType.PAUSE, issued_by=actor), actor=actor
        )

    async def resume(self, agent_ids: Iterable[str] | None, *, actor: str) -> CommandReport:
        """Resume monitoring. ``None`` = all PCs."""
        targets = self._resolve(agent_ids)
        if agent_ids is None:
            self._global_paused = False
            self._paused.clear()
        else:
            if self._global_paused:
                # Turn "all paused" into an explicit list minus the resumed PCs.
                self._global_paused = False
                self._paused.update(self._views)
            self._paused.difference_update(targets)
        self._persist_paused()
        return await self._dispatch(
            targets, CommandPayload(command=CommandType.RESUME, issued_by=actor), actor=actor
        )

    async def lock_screens(self, agent_ids: Iterable[str] | None, *, actor: str) -> CommandReport:
        return await self._dispatch(
            self._resolve(agent_ids),
            CommandPayload(command=CommandType.LOCK_SCREEN, issued_by=actor),
            actor=actor,
        )

    async def broadcast(
        self, agent_ids: Iterable[str] | None, text: str, *, actor: str
    ) -> CommandReport:
        text = text.strip()
        if not text:
            raise ValueError("Pesan tidak boleh kosong.")
        cmd = CommandPayload(
            command=CommandType.BROADCAST_MESSAGE, text=text[:1024], issued_by=actor
        )
        return await self._dispatch(
            self._resolve(agent_ids), cmd, actor=actor, details={"text": text[:200]}
        )

    # ------------------------------------------------------------ streaming
    async def set_viewing(self, *, grid_open: bool, focused_agent: str | None = None) -> None:
        """Tell agents how much screen to send, based on what the admin sees.

        Nobody viewing -> every agent stops capturing entirely (privacy + cost).
        Grid open -> thumbnails. One PC opened -> that PC streams hi-res.
        Stream level is not a security action, so it is sent without the
        ACK/audit machinery used for pause/resume.

        Coroutine (not a plain method) because the UI lives on another thread:
        it must be submitted to this service's event loop, never called directly.
        """
        self._grid_open = grid_open
        self._focused_agent = focused_agent
        if self._link is None:
            return
        for session in self._link.sessions():
            await self._send_stream_mode(session, self._desired_mode(session.agent_id))

    def _desired_mode(self, agent_id: str) -> StreamMode:
        if self._focused_agent == agent_id:
            return StreamMode.HIRES
        if self._grid_open:
            return StreamMode.GRID
        return StreamMode.OFF

    async def _send_stream_mode(self, session: AgentSessionLike, mode: StreamMode) -> None:
        if self._stream_modes.get(session.agent_id) is mode:
            return
        self._stream_modes[session.agent_id] = mode
        with contextlib.suppress(Exception):
            await session.send_command(
                CommandPayload(
                    command=CommandType.SET_STREAM, text=str(mode), issued_by=SYSTEM_ACTOR
                )
            )

    async def push_rules(self, ruleset: RuleSet) -> int:
        """Send the current ruleset to every connected PC. Returns PCs reached."""
        if self._link is None:
            return 0
        reached = 0
        for session in self._link.sessions():
            try:
                await session.send_ruleset(ruleset)
                reached += 1
            except Exception:
                log.warning("failed to push rules to %s", session.agent_id, exc_info=True)
        self._bus.publish(Topic.RULES_UPDATED, {"revision": ruleset.revision, "reached": reached})
        return reached

    # ------------------------------------------------------------ watchdog
    def check_heartbeats(self) -> list[str]:
        """Mark online PCs without a recent heartbeat as dropped. Returns them."""
        now = self._clock()
        dropped: list[str] = []
        for view in self._views.values():
            if (
                view.connection is ConnectionStatus.ONLINE
                and now - view.last_heartbeat > self._hb_timeout
            ):
                view.connection = ConnectionStatus.DROPPED
                dropped.append(view.agent_id)
                self._publish_agent(view)
        return dropped

    async def run_watchdog(self, interval_s: float = 2.0) -> None:
        while True:
            self.check_heartbeats()
            await asyncio.sleep(interval_s)

    # ------------------------------------------------------------ internals
    async def _dispatch(
        self,
        targets: list[str],
        cmd: CommandPayload,
        *,
        actor: str,
        audit_action: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> CommandReport:
        action = audit_action or f"command.{cmd.command}"
        target_label = "semua" if self._is_all(targets) else ",".join(sorted(targets))
        self._db.audit(
            actor=actor,
            action=action,
            target=target_label,
            details={"count": len(targets), **(details or {})},
            result="dikirim",
        )

        report = CommandReport(command=cmd.command, actor=actor)
        waits: dict[str, asyncio.Future[AckPayload]] = {}
        loop = asyncio.get_running_loop()

        for agent_id in targets:
            session = self._link.get(agent_id) if self._link else None
            now = self._clock()
            if session is None:
                cid = uuid.uuid4().hex
                self._db.add_command(command_id=cid, agent_id=agent_id,
                                     command=str(cmd.command), issued_by=actor, issued_at=now)
                self._db.finish_command(cid, status=str(CommandOutcome.UNREACHABLE), at=now)
                report.outcomes[agent_id] = CommandOutcome.UNREACHABLE
                continue
            fut: asyncio.Future[AckPayload] = loop.create_future()
            try:
                cid = await session.send_command(cmd)
            except Exception:
                log.warning("send %s to %s failed", cmd.command, agent_id, exc_info=True)
                cid = uuid.uuid4().hex
                self._db.add_command(command_id=cid, agent_id=agent_id,
                                     command=str(cmd.command), issued_by=actor, issued_at=now)
                self._db.finish_command(cid, status=str(CommandOutcome.UNREACHABLE), at=now)
                report.outcomes[agent_id] = CommandOutcome.UNREACHABLE
                continue
            self._db.add_command(command_id=cid, agent_id=agent_id, command=str(cmd.command),
                                 issued_by=actor, issued_at=now)
            self._pending[cid] = _Pending(agent_id=agent_id, command=cmd.command, future=fut)
            waits[cid] = fut
            self._bus.publish(Topic.COMMAND_UPDATED,
                              {"id": cid, "agent_id": agent_id, "status": "pending"})

        if waits:
            await asyncio.wait(list(waits.values()), timeout=self._cmd_timeout)

        for cid, fut in waits.items():
            pending = self._pending.pop(cid)
            outcome, state, message = self._outcome_of(fut)
            self._db.finish_command(cid, status=str(outcome), resulting_state=state,
                                    message=message, at=self._clock())
            report.outcomes[pending.agent_id] = outcome
            self._bus.publish(Topic.COMMAND_UPDATED,
                              {"id": cid, "agent_id": pending.agent_id, "status": str(outcome)})

        self._db.audit(
            actor=actor,
            action=f"{action}.hasil",
            target=target_label,
            details={o: sorted(a for a, x in report.outcomes.items() if str(x) == o)
                     for o in {str(x) for x in report.outcomes.values()}},
            result=report.summary(),
        )
        return report

    @staticmethod
    def _outcome_of(
        fut: asyncio.Future[AckPayload],
    ) -> tuple[CommandOutcome, str | None, str]:
        if not fut.done():
            fut.cancel()
            return CommandOutcome.TIMEOUT, None, "tidak ada balasan"
        if fut.cancelled() or fut.exception() is not None:
            return CommandOutcome.UNREACHABLE, None, "koneksi terputus"
        ack = fut.result()
        if not ack.ok:
            return CommandOutcome.FAILED, str(ack.resulting_state), ack.message
        return CommandOutcome.ACKED, str(ack.resulting_state), ack.message

    def _fail_pending_for(self, agent_id: str) -> None:
        for pending in self._pending.values():
            if pending.agent_id == agent_id and not pending.future.done():
                pending.future.set_exception(ConnectionError("agent disconnected"))

    def _resolve(self, agent_ids: Iterable[str] | None) -> list[str]:
        if agent_ids is None:
            return sorted(self._views)
        known = [a for a in dict.fromkeys(agent_ids) if a]
        if not known:
            raise ValueError("Tidak ada PC yang dipilih.")
        return known

    def _is_all(self, targets: list[str]) -> bool:
        return bool(self._views) and set(targets) == set(self._views)

    def _spawn(self, coro: Any) -> None:
        """Run a fire-and-forget coroutine, keeping a reference so it isn't GC'd."""
        task = asyncio.ensure_future(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def _persist_paused(self) -> None:
        self._db.set_setting(_SETTING_PAUSED, sorted(self._paused))
        self._db.set_setting(_SETTING_GLOBAL_PAUSED, self._global_paused)

    def _publish_agent(self, view: AgentView) -> None:
        self._bus.publish(Topic.AGENT_UPDATED, view.to_dict())

    def _load_known_agents(self) -> None:
        for row in self._db.list_agents():
            self._views[row["agent_id"]] = AgentView(
                agent_id=row["agent_id"],
                hostname=row["hostname"],
                username=row["username"],
                remote_addr=row["remote_addr"],
                agent_version=row["agent_version"],
                group=row["group_name"],
                connection=ConnectionStatus.OFFLINE,
                last_heartbeat=row["last_seen"],
            )

    def _save_screenshot(self, agent_id: str, b64: str | None, occurred_at: float) -> str | None:
        if not b64:
            return None
        try:
            data = base64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            log.warning("invalid screenshot encoding from %s", agent_id)
            return None
        if len(data) > MAX_SCREENSHOT_BYTES or data[:3] != b"\xff\xd8\xff":
            log.warning("rejected screenshot from %s (size/format)", agent_id)
            return None
        safe_id = "".join(c for c in agent_id if c.isalnum() or c in "-_")[:64] or "agent"
        day = time.strftime("%Y-%m-%d", time.localtime(occurred_at))
        path = self._shots / day / f"{safe_id}-{int(occurred_at * 1000)}-{uuid.uuid4().hex[:6]}.jpg"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError:
            log.warning("could not store screenshot for %s", agent_id, exc_info=True)
            return None
        return str(path)

    def purge_expired(self, retention_days: int) -> int:
        """Apply data retention: delete old rows and their screenshot files."""
        cutoff = self._clock() - retention_days * 86400
        paths = self._db.purge_older_than(cutoff)
        for p in paths:
            with contextlib.suppress(OSError):
                Path(p).unlink()
        return len(paths)
