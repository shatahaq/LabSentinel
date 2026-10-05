"""What a session report says, independent of how it is rendered.

The PDF and the Excel workbook are two views of the same :class:`SessionReport`,
so they can never disagree about the numbers. Building it only reads the
database; rendering never touches it.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from labsentinel_dashboard.domain.session_presets import PROFILE_LABELS
from labsentinel_dashboard.storage.db import AuditRow, Database, SessionRow, ViolationRow

# Plain-language labels, the same words the dashboard UI uses (ADR-0005).
KIND_LABELS: dict[str, str] = {
    "app_blocked": "Aplikasi tidak diizinkan",
    "app_closed": "Aplikasi ditutup",
    "impersonation": "Aplikasi menyamar",
    "site_title": "Dugaan situs terlarang",
    "agent_tamper": "Agent dihentikan tidak wajar",
}

STATUS_LABELS: dict[str, str] = {
    "scheduled": "Terjadwal",
    "active": "Berjalan",
    "ended": "Selesai",
    "cancelled": "Dibatalkan",
}

ACTION_LABELS: dict[str, str] = {
    "command.pause": "Menjeda PC",
    "command.pause.reapply": "Menjeda ulang PC yang tersambung kembali",
    "command.resume": "Melanjutkan PC",
    "command.lock_screen": "Mengunci layar",
    "command.broadcast_message": "Mengirim pesan",
    "command.set_dry_run": "Mengubah mode uji coba",
    "command.set_stream": "Mengatur tampilan layar",
    "rules.update": "Mengubah aturan",
    "session.start": "Memulai sesi",
    "session.end": "Mengakhiri sesi",
    "session.schedule": "Menjadwalkan sesi",
    "session.cancel": "Membatalkan sesi",
    "session.missed": "Sesi terjadwal terlewat",
    "pairing.success": "PC dipasangkan",
    "pairing.failed": "Kode pemasangan salah",
    "pairing.refused": "PC baru ditolak (pendaftaran ditutup)",
    "pairing.rotate": "Membuat kode pemasangan baru",
    "pairing.open": "Membuka pendaftaran PC baru",
    "pairing.close": "Menutup pendaftaran PC baru",
    "pairing.revoke": "Mencabut pemasangan PC",
    "report.export": "Mengunduh laporan sesi",
}

# Upper bound on rows read for one session; a 3-hour exam in a 40-PC lab is
# nowhere near this, and it keeps a runaway agent from exhausting memory.
MAX_VIOLATIONS = 50_000


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


def action_label(action: str) -> str:
    if action in ACTION_LABELS:
        return ACTION_LABELS[action]
    if action.endswith(".hasil"):
        base = ACTION_LABELS.get(action.removesuffix(".hasil"))
        if base:
            return f"Hasil: {base[0].lower()}{base[1:]}"
    return action


def mode_label(mode: str) -> str:
    return PROFILE_LABELS.get(mode, {}).get("label", mode)


@dataclass(frozen=True)
class PcSummary:
    agent_id: str
    hostname: str
    username: str
    total: int
    enforced: int  # counted outside dry-run (action could be taken)
    by_kind: dict[str, int]
    first_at: float
    last_at: float


@dataclass(frozen=True)
class SessionReport:
    session: SessionRow
    generated_at: float
    window_start: float
    window_end: float
    violations: list[ViolationRow]  # oldest first
    audit: list[AuditRow]  # oldest first, within the session window
    pcs: list[PcSummary]  # most violations first
    registered_pcs: int
    totals_by_kind: dict[str, int] = field(default_factory=dict)

    # ---------------------------------------------------------------- facts
    @property
    def total(self) -> int:
        return len(self.violations)

    @property
    def dry_run_count(self) -> int:
        return sum(1 for v in self.violations if v.dry_run)

    @property
    def title_based_count(self) -> int:
        return sum(1 for v in self.violations if v.detected_from_title)

    @property
    def tamper_count(self) -> int:
        return self.totals_by_kind.get("agent_tamper", 0)

    @property
    def duration_s(self) -> float:
        return max(0.0, self.window_end - self.window_start)

    @property
    def still_running(self) -> bool:
        return self.session.status == "active"

    def filename(self, ext: str) -> str:
        """``laporan-sesi-12-uts-pemrograman-2026-10-05.pdf``"""
        day = datetime.fromtimestamp(self.window_start).strftime("%Y-%m-%d")
        slug = re.sub(r"[^a-z0-9]+", "-", self.session.name.lower()).strip("-")[:40] or "sesi"
        return f"laporan-sesi-{self.session.id}-{slug}-{day}.{ext}"


class ReportError(ValueError):
    """A report cannot be made for this session (shown to the admin as is)."""


def build_session_report(
    db: Database, session_id: int, *, now: float | None = None
) -> SessionReport:
    """Collect everything a session report shows. Read-only."""
    current = time.time() if now is None else now
    session = db.get_session(session_id)
    if session is None:
        raise ReportError("Sesi tidak ditemukan.")
    if session.started_at is None:
        raise ReportError("Sesi ini belum pernah dimulai, jadi belum ada yang bisa dilaporkan.")

    start = session.started_at
    end = session.ended_at if session.ended_at is not None else current
    violations = sorted(
        db.list_violations(session_id=session_id, limit=MAX_VIOLATIONS),
        key=lambda v: (v.occurred_at, v.id),
    )
    audit = db.list_audit_between(start, end)

    usernames = {str(a["agent_id"]): str(a.get("username") or "") for a in db.list_agents()}
    per_pc: dict[str, list[ViolationRow]] = {}
    for v in violations:
        per_pc.setdefault(v.agent_id, []).append(v)
    pcs = [
        PcSummary(
            agent_id=agent_id,
            hostname=rows[-1].hostname or agent_id,
            username=usernames.get(agent_id, ""),
            total=len(rows),
            enforced=sum(1 for r in rows if not r.dry_run),
            by_kind=dict(Counter(r.kind for r in rows)),
            first_at=rows[0].occurred_at,
            last_at=rows[-1].occurred_at,
        )
        for agent_id, rows in per_pc.items()
    ]
    pcs.sort(key=lambda p: (-p.total, p.hostname.lower()))

    return SessionReport(
        session=session,
        generated_at=current,
        window_start=start,
        window_end=end,
        violations=violations,
        audit=audit,
        pcs=pcs,
        registered_pcs=len(db.paired_agent_ids()),
        totals_by_kind=dict(Counter(v.kind for v in violations)),
    )


# ------------------------------------------------------------ formatting
def fmt_datetime(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y %H:%M") if ts else "-"


def fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def fmt_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} jam {minutes} menit"
    return f"{minutes} menit"
