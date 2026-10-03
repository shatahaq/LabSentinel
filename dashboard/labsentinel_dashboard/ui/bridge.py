"""Bridge between the Eel/gevent UI thread and the asyncio domain thread.

Eel runs the UI on gevent in the main thread while the agent-facing WebSocket
server runs an asyncio loop in another thread. Rather than calling across
threads in both directions (which is where gevent/asyncio integrations usually
break), this bridge keeps the rules simple:

* UI -> domain: coroutines are submitted to the asyncio loop with
  ``run_coroutine_threadsafe`` and awaited with a timeout.
* domain -> UI: events are appended to a bounded, lock-protected buffer that the
  UI drains by polling. Nothing calls into gevent from the asyncio thread.

The buffer is bounded so a UI that stops polling (closed window, frozen tab)
can never grow memory without limit.

Every method here is a boundary: it validates its inputs, never raises into the
UI, and returns plain JSON-serializable data with a ``ok``/``error`` shape so
the frontend can always show a meaningful message.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Coroutine
from typing import Any

from labsentinel_dashboard.domain.events import EventBus
from labsentinel_dashboard.domain.rules_service import RulesService
from labsentinel_dashboard.domain.service import CommandReport, DashboardService
from labsentinel_dashboard.domain.session_presets import PROFILE_LABELS
from labsentinel_dashboard.domain.sessions import SessionError, SessionService
from labsentinel_dashboard.domain.site_presets import ALL_CATEGORIES, BY_KEY, rules_for
from labsentinel_dashboard.storage.db import Database
from labsentinel_shared.enums import EnforcementMode
from labsentinel_shared.rules import AppRule, RuleSet, SiteRule

log = logging.getLogger("labsentinel.dashboard.ui")

MAX_BUFFERED_EVENTS = 2000
CALL_TIMEOUT_S = 20.0


class UiBridge:
    def __init__(
        self,
        *,
        service: DashboardService,
        sessions: SessionService,
        rules: RulesService,
        db: Database,
        bus: EventBus,
        loop: asyncio.AbstractEventLoop,
        actor: str = "pengawas",
    ) -> None:
        self._svc = service
        self._sessions = sessions
        self._rules = rules
        self._db = db
        self._loop = loop
        self._actor = actor
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_BUFFERED_EVENTS)
        self._lock = threading.Lock()
        self._seq = 0
        bus.subscribe(self._on_event)

    # ------------------------------------------------------------ plumbing
    def _on_event(self, topic: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._seq += 1
            self._events.append({"seq": self._seq, "topic": topic, "data": data})

    def _run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Run a domain coroutine from the UI thread and wait for the result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=CALL_TIMEOUT_S)

    @staticmethod
    def _ok(**payload: Any) -> dict[str, Any]:
        return {"ok": True, **payload}

    @staticmethod
    def _err(message: str) -> dict[str, Any]:
        return {"ok": False, "error": message}

    @staticmethod
    def _report(report: CommandReport) -> dict[str, Any]:
        return {
            "ok": True,
            "command": str(report.command),
            "total": len(report.outcomes),
            "succeeded": report.succeeded,
            "failed": report.failed,
            "outcomes": {a: str(o) for a, o in report.outcomes.items()},
            "summary": report.summary(),
        }

    # ------------------------------------------------------------ polling
    def poll_events(self, since_seq: int = 0) -> dict[str, Any]:
        """Drain events newer than ``since_seq``. The UI calls this on a timer."""
        with self._lock:
            events = [e for e in self._events if e["seq"] > since_seq]
            latest = self._seq
        return {"ok": True, "events": events, "seq": latest}

    # ------------------------------------------------------------ snapshots
    def get_overview(self) -> dict[str, Any]:
        try:
            session = self._sessions.active()
            return self._ok(
                counts=self._svc.counts(),
                agents=[a.to_dict() for a in self._svc.agents()],
                session=_session_dict(session),
                upcoming=[_session_dict(s) for s in self._sessions.upcoming()[:5]],
                rules=_rules_dict(self._rules.current()),
                global_paused=self._svc.global_paused,
                recent_violations=[
                    _violation_dict(v) for v in self._db.list_violations(limit=12)
                ],
            )
        except Exception:
            log.exception("get_overview failed")
            return self._err("Gagal memuat ringkasan.")

    def get_agents(self) -> dict[str, Any]:
        try:
            return self._ok(
                agents=[a.to_dict() for a in self._svc.agents()], counts=self._svc.counts()
            )
        except Exception:
            log.exception("get_agents failed")
            return self._err("Gagal memuat daftar PC.")

    def get_frame(self, agent_id: str) -> dict[str, Any]:
        """Latest screen frame for one PC (base64 JPEG), if any."""
        frame = self._svc.latest_frame(str(agent_id))
        if frame is None:
            return self._ok(frame=None)
        return self._ok(
            frame={
                "jpeg_b64": frame.jpeg_b64,
                "width": frame.width,
                "height": frame.height,
                "hi_res": frame.hi_res,
                "captured_at": frame.captured_at,
                "age_s": max(0.0, time.time() - frame.captured_at),
            }
        )

    def set_viewing(self, grid_open: bool, focused_agent: str | None = None) -> dict[str, Any]:
        """Tell agents what the admin is looking at (drives streaming level)."""
        try:
            self._run(
                self._svc.set_viewing(
                    grid_open=bool(grid_open),
                    focused_agent=str(focused_agent) if focused_agent else None,
                )
            )
            return self._ok()
        except Exception:
            log.exception("set_viewing failed")
            return self._err("Gagal mengatur tampilan.")

    # ------------------------------------------------------------ control
    def pause(self, agent_ids: list[str] | None = None) -> dict[str, Any]:
        return self._command(lambda: self._svc.pause(agent_ids, actor=self._actor))

    def resume(self, agent_ids: list[str] | None = None) -> dict[str, Any]:
        return self._command(lambda: self._svc.resume(agent_ids, actor=self._actor))

    def lock_screens(self, agent_ids: list[str] | None = None) -> dict[str, Any]:
        return self._command(lambda: self._svc.lock_screens(agent_ids, actor=self._actor))

    def broadcast(self, text: str, agent_ids: list[str] | None = None) -> dict[str, Any]:
        return self._command(lambda: self._svc.broadcast(agent_ids, str(text), actor=self._actor))

    def _command(self, make_coro: Any) -> dict[str, Any]:
        try:
            report = self._run(make_coro())
            return self._report(report)
        except ValueError as exc:
            return self._err(str(exc))
        except TimeoutError:
            return self._err("Perintah terlalu lama diproses. Coba lagi.")
        except Exception:
            log.exception("command failed")
            return self._err("Perintah gagal dikirim.")

    # ------------------------------------------------------------ rules
    def get_rules(self) -> dict[str, Any]:
        return self._ok(
            rules=_rules_dict(self._rules.current()),
            categories=[c.to_dict() for c in ALL_CATEGORIES],
            modes=[{"key": k, **v} for k, v in PROFILE_LABELS.items()],
        )

    def set_mode(self, mode: str) -> dict[str, Any]:
        try:
            enforcement = EnforcementMode(str(mode))
        except ValueError:
            return self._err("Mode tidak dikenal.")
        try:
            ruleset = self._rules.update(actor=self._actor, mode=enforcement)
            reached = self._run(self._svc.push_rules(ruleset))
            return self._ok(rules=_rules_dict(ruleset), reached=reached)
        except Exception:
            log.exception("set_mode failed")
            return self._err("Gagal mengganti mode.")

    def set_dry_run(self, enabled: bool) -> dict[str, Any]:
        return self._update_rules(dry_run=bool(enabled))

    def set_whitelist(self, names: list[str]) -> dict[str, Any]:
        try:
            apps = tuple(
                AppRule(process_name=n) for n in dict.fromkeys(names) if str(n).strip()
            )
        except Exception:
            return self._err("Daftar aplikasi tidak valid.")
        return self._update_rules(app_whitelist=apps)

    def set_site_categories(self, keys: list[str]) -> dict[str, Any]:
        unknown = [k for k in keys if k not in BY_KEY]
        if unknown:
            return self._err(f"Kategori tidak dikenal: {', '.join(unknown)}")
        return self._update_rules(site_blocklist=rules_for(list(keys)))

    def set_custom_sites(self, patterns: list[str]) -> dict[str, Any]:
        """Replace the blocklist with explicit patterns (import / manual edit)."""
        try:
            rules = tuple(
                SiteRule(pattern=p.strip(), category="kustom")
                for p in dict.fromkeys(patterns)
                if str(p).strip()
            )
        except Exception:
            return self._err("Daftar situs tidak valid.")
        return self._update_rules(site_blocklist=rules)

    def set_close_grace(self, seconds: int) -> dict[str, Any]:
        try:
            value = int(seconds)
        except (TypeError, ValueError):
            return self._err("Jeda harus berupa angka.")
        if not 0 <= value <= 60:
            return self._err("Jeda harus antara 0 dan 60 detik.")
        return self._update_rules(close_grace_seconds=value)

    def _update_rules(self, **changes: Any) -> dict[str, Any]:
        try:
            ruleset = self._rules.update(actor=self._actor, **changes)
            reached = self._run(self._svc.push_rules(ruleset))
            return self._ok(rules=_rules_dict(ruleset), reached=reached)
        except Exception:
            log.exception("rule update failed")
            return self._err("Gagal menyimpan aturan.")

    # ------------------------------------------------------------ sessions
    def start_session(self, name: str, mode: str) -> dict[str, Any]:
        try:
            row = self._run(
                self._sessions.start_now(
                    name=str(name), mode=EnforcementMode(str(mode)), actor=self._actor
                )
            )
            return self._ok(session=_session_dict(row))
        except (SessionError, ValueError) as exc:
            return self._err(str(exc))
        except Exception:
            log.exception("start_session failed")
            return self._err("Gagal memulai sesi.")

    def end_session(self) -> dict[str, Any]:
        try:
            row = self._run(self._sessions.end_active(actor=self._actor))
            return self._ok(session=_session_dict(row))
        except Exception:
            log.exception("end_session failed")
            return self._err("Gagal mengakhiri sesi.")

    def schedule_session(
        self, name: str, mode: str, start_at: float, end_at: float
    ) -> dict[str, Any]:
        try:
            row = self._sessions.schedule(
                name=str(name), mode=EnforcementMode(str(mode)),
                start_at=float(start_at), end_at=float(end_at), actor=self._actor,
            )
            return self._ok(session=_session_dict(row))
        except (SessionError, ValueError) as exc:
            return self._err(str(exc))
        except Exception:
            log.exception("schedule_session failed")
            return self._err("Gagal menjadwalkan sesi.")

    def cancel_session(self, session_id: int) -> dict[str, Any]:
        try:
            self._sessions.cancel(int(session_id), actor=self._actor)
            return self._ok()
        except (SessionError, ValueError) as exc:
            return self._err(str(exc))
        except Exception:
            log.exception("cancel_session failed")
            return self._err("Gagal membatalkan sesi.")

    def list_sessions(self) -> dict[str, Any]:
        return self._ok(sessions=[_session_dict(s) for s in self._db.list_sessions(limit=100)])

    # ------------------------------------------------------------ history
    def get_violations(self, session_id: int | None = None, limit: int = 200) -> dict[str, Any]:
        try:
            rows = self._db.list_violations(
                session_id=int(session_id) if session_id else None,
                limit=max(1, min(int(limit), 1000)),
            )
            return self._ok(violations=[_violation_dict(v) for v in rows])
        except Exception:
            log.exception("get_violations failed")
            return self._err("Gagal memuat riwayat pelanggaran.")

    def get_screenshot(self, violation_id: int) -> dict[str, Any]:
        """Base64 JPEG evidence for one violation, read on demand."""
        import base64
        from pathlib import Path

        try:
            match = [v for v in self._db.list_violations(limit=1000) if v.id == int(violation_id)]
            if not match or not match[0].screenshot_path:
                return self._ok(image=None)
            data = Path(match[0].screenshot_path).read_bytes()
            return self._ok(image=base64.b64encode(data).decode("ascii"))
        except OSError:
            return self._err("Berkas bukti tidak ditemukan.")
        except Exception:
            log.exception("get_screenshot failed")
            return self._err("Gagal memuat bukti.")

    def get_audit(self, limit: int = 300) -> dict[str, Any]:
        try:
            rows = self._db.list_audit(limit=max(1, min(int(limit), 2000)))
            return self._ok(
                entries=[
                    {
                        "id": r.id, "ts": r.ts, "actor": r.actor, "action": r.action,
                        "target": r.target, "details": r.details, "result": r.result,
                    }
                    for r in rows
                ]
            )
        except Exception:
            log.exception("get_audit failed")
            return self._err("Gagal memuat catatan aktivitas.")


# --------------------------------------------------------------- serializers
def _rules_dict(rs: RuleSet) -> dict[str, Any]:
    return {
        "revision": rs.revision,
        "mode": str(rs.mode),
        "dry_run": rs.dry_run,
        "close_grace_seconds": rs.close_grace_seconds,
        "block_incognito": rs.block_incognito,
        "app_whitelist": [r.process_name for r in rs.app_whitelist],
        "site_count": len(rs.site_blocklist),
        "site_patterns": [r.pattern for r in rs.site_blocklist],
        "site_categories": sorted({r.category for r in rs.site_blocklist}),
    }


def _session_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row.id, "name": row.name, "mode": row.mode, "status": row.status,
        "scheduled_start": row.scheduled_start, "scheduled_end": row.scheduled_end,
        "started_at": row.started_at, "ended_at": row.ended_at,
    }


def _violation_dict(v: Any) -> dict[str, Any]:
    return {
        "id": v.id, "agent_id": v.agent_id, "hostname": v.hostname, "kind": v.kind,
        "subject": v.subject, "detail": v.detail,
        "detected_from_title": v.detected_from_title, "dry_run": v.dry_run,
        "occurred_at": v.occurred_at, "has_screenshot": bool(v.screenshot_path),
        "session_id": v.session_id,
    }
