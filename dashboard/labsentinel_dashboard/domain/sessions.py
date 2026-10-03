"""Lab sessions (praktikum/ujian): start/end now or on a schedule.

Starting a session applies its mode, links new violations to it, and resumes
monitoring on all PCs. Ending it pauses monitoring on all PCs. Scheduled
sessions are started/ended automatically by :meth:`SessionService.tick`.
Every transition is audited, including automatic ones (actor = "sistem").
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from labsentinel_dashboard.domain.events import EventBus, Topic
from labsentinel_dashboard.domain.rules_service import RulesService
from labsentinel_dashboard.domain.service import SYSTEM_ACTOR, DashboardService
from labsentinel_dashboard.storage.db import Database, SessionRow
from labsentinel_shared.enums import EnforcementMode

log = logging.getLogger("labsentinel.dashboard.sessions")


class SessionError(ValueError):
    """User-facing validation error (message is shown in the UI)."""


class SessionService:
    def __init__(
        self,
        *,
        db: Database,
        service: DashboardService,
        rules: RulesService,
        bus: EventBus,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._db = db
        self._svc = service
        self._rules = rules
        self._bus = bus
        self._clock = clock
        active = db.list_sessions(status="active", limit=1)
        self._svc.active_session_id = active[0].id if active else None

    def active(self) -> SessionRow | None:
        sid = self._svc.active_session_id
        return self._db.get_session(sid) if sid is not None else None

    def upcoming(self) -> list[SessionRow]:
        return sorted(
            self._db.list_sessions(status="scheduled"),
            key=lambda s: s.scheduled_start or 0.0,
        )

    async def start_now(
        self, *, name: str, mode: EnforcementMode, actor: str, end_at: float | None = None
    ) -> SessionRow:
        name = name.strip()
        if not name:
            raise SessionError("Nama sesi wajib diisi.")
        if self._svc.active_session_id is not None:
            raise SessionError("Masih ada sesi yang berjalan. Akhiri sesi itu terlebih dahulu.")
        now = self._clock()
        if end_at is not None and end_at <= now:
            raise SessionError("Waktu selesai harus di masa depan.")
        sid = self._db.create_session(
            name=name, mode=str(mode), status="active", created_by=actor,
            scheduled_end=end_at, started_at=now,
        )
        return await self._activate(sid, mode=mode, actor=actor)

    def schedule(
        self, *, name: str, mode: EnforcementMode, start_at: float, end_at: float, actor: str
    ) -> SessionRow:
        name = name.strip()
        if not name:
            raise SessionError("Nama sesi wajib diisi.")
        if end_at <= start_at:
            raise SessionError("Waktu selesai harus setelah waktu mulai.")
        if end_at <= self._clock():
            raise SessionError("Jadwal sudah lewat.")
        for other in self.upcoming():
            if (
                other.scheduled_start is not None
                and other.scheduled_end is not None
                and start_at < other.scheduled_end
                and other.scheduled_start < end_at
            ):
                raise SessionError(f"Jadwal bentrok dengan sesi '{other.name}'.")
        sid = self._db.create_session(
            name=name, mode=str(mode), status="scheduled", created_by=actor,
            scheduled_start=start_at, scheduled_end=end_at,
        )
        self._db.audit(actor=actor, action="session.schedule", target=name,
                       details={"id": sid, "mode": str(mode), "start": start_at, "end": end_at},
                       result="ok")
        row = self._db.get_session(sid)
        assert row is not None
        self._publish(row)
        return row

    def cancel(self, session_id: int, *, actor: str) -> None:
        row = self._db.get_session(session_id)
        if row is None or row.status != "scheduled":
            raise SessionError("Hanya sesi terjadwal yang bisa dibatalkan.")
        self._db.set_session_status(session_id, "cancelled")
        self._db.audit(actor=actor, action="session.cancel", target=row.name,
                       details={"id": session_id}, result="ok")
        updated = self._db.get_session(session_id)
        if updated:
            self._publish(updated)

    async def end_active(self, *, actor: str) -> SessionRow | None:
        sid = self._svc.active_session_id
        if sid is None:
            return None
        self._db.set_session_status(sid, "ended", ended_at=self._clock())
        self._svc.active_session_id = None
        row = self._db.get_session(sid)
        self._db.audit(actor=actor, action="session.end", target=row.name if row else str(sid),
                       details={"id": sid}, result="ok")
        await self._svc.pause(None, actor=actor)
        if row:
            self._publish(row)
        return row

    async def tick(self) -> None:
        """Start/end scheduled sessions whose time has come."""
        now = self._clock()
        active = self.active()
        if active and active.scheduled_end is not None and now >= active.scheduled_end:
            log.info("auto-ending session %s", active.name)
            await self.end_active(actor=SYSTEM_ACTOR)
            active = None
        if active is None:
            for row in self.upcoming():
                if row.scheduled_end is not None and now >= row.scheduled_end:
                    # Missed entirely (e.g. dashboard was off): don't start it late.
                    self._db.set_session_status(row.id, "cancelled")
                    self._db.audit(actor=SYSTEM_ACTOR, action="session.missed",
                                   target=row.name, details={"id": row.id}, result="dilewati")
                    continue
                if row.scheduled_start is not None and now >= row.scheduled_start:
                    log.info("auto-starting session %s", row.name)
                    self._db.set_session_status(row.id, "active", started_at=now)
                    await self._activate(row.id, mode=EnforcementMode(row.mode),
                                         actor=SYSTEM_ACTOR)
                    break

    async def run(self, interval_s: float = 5.0) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("session scheduler tick failed")
            await asyncio.sleep(interval_s)

    async def _activate(self, sid: int, *, mode: EnforcementMode, actor: str) -> SessionRow:
        self._svc.active_session_id = sid
        row = self._db.get_session(sid)
        assert row is not None
        self._db.audit(actor=actor, action="session.start", target=row.name,
                       details={"id": sid, "mode": str(mode)}, result="ok")
        if self._rules.current().mode is not mode:
            ruleset = self._rules.update(actor=actor, mode=mode)
            await self._svc.push_rules(ruleset)
        await self._svc.resume(None, actor=actor)
        self._publish(row)
        return row

    def _publish(self, row: SessionRow) -> None:
        self._bus.publish(Topic.SESSION_UPDATED, {
            "id": row.id, "name": row.name, "mode": row.mode, "status": row.status,
            "scheduled_start": row.scheduled_start, "scheduled_end": row.scheduled_end,
            "started_at": row.started_at, "ended_at": row.ended_at,
        })
