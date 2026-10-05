"""Session reports: the data model, the PDF and the Excel workbook."""

from __future__ import annotations

import io
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook
from PIL import Image
from pypdf import PdfReader

from labsentinel_dashboard.reports import ReportError, build_session_report
from labsentinel_dashboard.reports.pdf import render_pdf
from labsentinel_dashboard.reports.xlsx import render_xlsx
from labsentinel_dashboard.storage.db import Database

T0 = datetime(2026, 10, 5, 8, 0).timestamp()


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    d = Database(tmp_path / "r.sqlite")
    yield d
    d.close()


def _shot(path: Path, size: tuple[int, int] = (640, 360)) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (40, 60, 90)).save(path, format="JPEG")
    return str(path)


def _violation(db: Database, session_id: int | None, agent: str, host: str, kind: str,
               subject: str, at: float, **kw: object) -> None:
    db.add_violation(
        agent_id=agent, hostname=host, kind=kind, subject=subject,
        detail=str(kw.get("detail", "")), detected_from_title=bool(kw.get("title", False)),
        dry_run=bool(kw.get("dry_run", False)), occurred_at=at,
        screenshot_path=kw.get("shot"),  # type: ignore[arg-type]
        session_id=session_id,
    )


@pytest.fixture
def session_id(db: Database, tmp_path: Path) -> int:
    sid = db.create_session(name="UTS Pemrograman Dasar", mode="exam", status="active",
                            created_by="pengawas", started_at=T0)
    db.set_session_status(sid, "ended", ended_at=T0 + 2 * 3600 + 15 * 60)
    for agent in ("lab-a-01", "lab-a-02", "lab-a-03"):
        db.set_agent_token_hash(agent, "x")
    db.upsert_agent(agent_id="lab-a-01", hostname="LAB-A-01", username="andi",
                    agent_version="0.1.0", remote_addr="10.0.0.11")

    _violation(db, sid, "lab-a-01", "LAB-A-01", "app_closed", "steam.exe", T0 + 600,
               detail="'steam.exe' tidak ada dalam whitelist",
               shot=_shot(tmp_path / "shots" / "a.jpg"))
    _violation(db, sid, "lab-a-01", "LAB-A-01", "site_title", "chat.openai.com", T0 + 1200,
               detail="Judul jendela: ChatGPT - Google Chrome", title=True,
               shot=_shot(tmp_path / "shots" / "b.jpg", (360, 640)))  # portrait screen
    _violation(db, sid, "lab-a-02", "LAB-A-02", "app_blocked", "discord.exe", T0 + 900,
               dry_run=True, shot=str(tmp_path / "shots" / "missing.jpg"))
    _violation(db, sid, "lab-a-01", "LAB-A-01", "agent_tamper", "agent", T0 + 3000,
               detail="Agent dihentikan paksa")
    _violation(db, sid, "lab-a-03", "LAB-A-03", "app_closed", "微信.exe", T0 + 3100)
    # Another session's record must not leak into this report.
    _violation(db, None, "lab-a-02", "LAB-A-02", "app_closed", "other.exe", T0 - 600)

    db.audit(actor="pengawas", action="session.start", target="UTS", ts=T0 + 1)
    db.audit(actor="pengawas", action="command.pause", target="2 PC", result="2/2", ts=T0 + 50)
    db.audit(actor="pengawas", action="rules.update", ts=T0 - 3600)  # before: excluded
    return sid


# ------------------------------------------------------------- model
def test_report_collects_only_this_session(db: Database, session_id: int) -> None:
    r = build_session_report(db, session_id, now=T0 + 10 * 3600)
    assert r.total == 5
    assert r.violations[0].subject == "steam.exe"  # oldest first
    assert "other.exe" not in {v.subject for v in r.violations}
    assert [a.action for a in r.audit] == ["session.start", "command.pause"]
    assert r.registered_pcs == 3
    assert r.dry_run_count == 1
    assert r.title_based_count == 1
    assert r.tamper_count == 1
    assert r.duration_s == 2 * 3600 + 15 * 60
    assert not r.still_running


def test_per_pc_summary_is_ordered_by_count(db: Database, session_id: int) -> None:
    r = build_session_report(db, session_id)
    assert [p.hostname for p in r.pcs] == ["LAB-A-01", "LAB-A-02", "LAB-A-03"]
    top = r.pcs[0]
    assert (top.total, top.enforced, top.username) == (3, 3, "andi")
    assert top.by_kind == {"app_closed": 1, "site_title": 1, "agent_tamper": 1}
    assert r.pcs[1].enforced == 0  # its only record was dry-run


def test_running_session_reports_up_to_now(db: Database) -> None:
    sid = db.create_session(name="Praktikum", mode="lab", status="active",
                            created_by="x", started_at=T0)
    r = build_session_report(db, sid, now=T0 + 1800)
    assert r.still_running
    assert r.window_end == T0 + 1800


def test_unknown_or_unstarted_session_is_refused(db: Database) -> None:
    with pytest.raises(ReportError, match="tidak ditemukan"):
        build_session_report(db, 999)
    sid = db.create_session(name="Besok", mode="exam", status="scheduled", created_by="x",
                            scheduled_start=T0 + 86400, scheduled_end=T0 + 90000)
    with pytest.raises(ReportError, match="belum pernah dimulai"):
        build_session_report(db, sid)


def test_filename_is_safe_and_descriptive(db: Database, session_id: int) -> None:
    r = build_session_report(db, session_id)
    assert r.filename("pdf") == f"laporan-sesi-{session_id}-uts-pemrograman-dasar-2026-10-05.pdf"
    db.create_session(name="../../etc/passwd", mode="exam", status="active", created_by="x",
                      started_at=T0)
    evil = build_session_report(db, session_id + 1)
    assert "/" not in evil.filename("pdf") and ".." not in evil.filename("pdf")


# --------------------------------------------------------------- PDF
def _pdf_text(data: bytes) -> str:
    reader = PdfReader(io.BytesIO(data))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def test_pdf_contains_the_story(db: Database, session_id: int) -> None:
    data = render_pdf(build_session_report(db, session_id))
    assert data.startswith(b"%PDF")
    text = _pdf_text(data)
    for expected in (
        "UTS Pemrograman Dasar", "Ujian", "2 jam 15 menit",
        "LAB-A-01", "LAB-A-02", "steam.exe", "chat.openai.com",
        "Aplikasi ditutup", "Dugaan situs terlarang", "Agent dihentikan tidak wajar",
        "mode uji coba", "judul", "Menjeda PC", "Halaman 1 dari",
        "Berkas bukti tidak ditemukan",  # missing evidence file is said, not crashed on
    ):
        assert expected in text, expected
    assert "other.exe" not in text


def test_pdf_embeds_evidence_images(db: Database, session_id: int) -> None:
    reader = PdfReader(io.BytesIO(render_pdf(build_session_report(db, session_id))))
    images = sum(len(page.images) for page in reader.pages)
    assert images == 2  # two readable screenshots; the missing one is text


def test_pdf_caps_evidence_and_says_so(db: Database, session_id: int) -> None:
    data = render_pdf(build_session_report(db, session_id), max_evidence=1)
    assert "Menampilkan 1 dari 3 bukti" in _pdf_text(data)


def test_pdf_for_a_quiet_session(db: Database) -> None:
    sid = db.create_session(name="Tenang", mode="lab", status="active", created_by="x",
                            started_at=T0)
    text = _pdf_text(render_pdf(build_session_report(db, sid, now=T0 + 600)))
    assert "Tidak ada pelanggaran tercatat" in text
    assert "masih berjalan" in text


def test_pdf_survives_hostile_text(db: Database) -> None:
    sid = db.create_session(name="<b>x</b> & y", mode="exam", status="active", created_by="x",
                            started_at=T0)
    _violation(db, sid, "pc", "PC", "site_title", "<script>alert(1)</script> &amp;", T0 + 1,
               detail="</para><font size=40>")
    data = render_pdf(build_session_report(db, sid, now=T0 + 60))
    # Long cells wrap, and where a wrap lands depends on the font found on the
    # machine (Arial on Windows, DejaVu here); compare without whitespace.
    text = "".join(_pdf_text(data).split())
    # Markup in names and titles is printed literally, never interpreted.
    assert "<script>alert(1)</script>&amp;" in text
    assert "</para><fontsize=40>" in text
    assert "<b>x</b>&y" in text


# -------------------------------------------------------------- Excel
def test_xlsx_has_real_dates_filters_and_every_record(db: Database, session_id: int) -> None:
    data = render_xlsx(build_session_report(db, session_id))
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["Ringkasan", "Per PC", "Pelanggaran", "Catatan aktivitas"]

    ws = wb["Pelanggaran"]
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    assert len(rows) == 5
    assert isinstance(rows[0][0], datetime)
    assert rows[0][0] == datetime.fromtimestamp(T0 + 600)
    assert rows[0][4] == "steam.exe"
    assert ws.freeze_panes == "A2"
    assert ws.auto_filter.ref
    assert any(r[4] == "微信.exe" for r in rows)

    per_pc = list(wb["Per PC"].iter_rows(min_row=1, values_only=True))
    assert per_pc[1][0] == "LAB-A-01" and per_pc[1][3] == 3

    summary = {r[0]: r[1] for r in wb["Ringkasan"].iter_rows(values_only=True) if r and r[0]}
    assert summary["Total pelanggaran"] == 5
    assert summary["Hanya dicatat (mode uji coba)"] == 1
    assert summary["Mode"] == "Ujian"

    audit = list(wb["Catatan aktivitas"].iter_rows(min_row=2, values_only=True))
    assert [a[2] for a in audit] == ["Memulai sesi", "Menjeda PC"]


def test_xlsx_for_a_quiet_session(db: Database) -> None:
    sid = db.create_session(name="Tenang", mode="lab", status="active", created_by="x",
                            started_at=T0)
    wb = load_workbook(io.BytesIO(render_xlsx(build_session_report(db, sid, now=T0 + 60))))
    assert wb["Pelanggaran"].max_row == 1  # header only


# ------------------------------------------------------------- UI bridge
def _bridge(db: Database, reports_dir: Path | None) -> object:
    import asyncio

    from labsentinel_dashboard.domain.events import EventBus
    from labsentinel_dashboard.ui.bridge import UiBridge

    loop = asyncio.new_event_loop()
    loop.close()
    unused: object = None
    return UiBridge(service=unused, sessions=unused, rules=unused, db=db,  # type: ignore[arg-type]
                    bus=EventBus(), loop=loop, reports_dir=reports_dir)


@pytest.mark.parametrize(("fmt", "magic"), [("pdf", b"%PDF"), ("xlsx", b"PK")])
def test_bridge_exports_download_and_keeps_a_copy(
    db: Database, session_id: int, tmp_path: Path, fmt: str, magic: bytes
) -> None:
    import base64

    bridge = _bridge(db, tmp_path / "reports")
    res = bridge.export_report(session_id, fmt)  # type: ignore[attr-defined]
    assert res["ok"], res
    data = base64.b64decode(res["b64"])
    assert data.startswith(magic)
    assert res["filename"].endswith(f".{fmt}")
    assert Path(res["saved_to"]).read_bytes() == data
    entry = next(a for a in db.list_audit() if a.action == "report.export")
    assert entry.details == {"session_id": session_id, "format": fmt}


def test_bridge_export_reports_problems_plainly(db: Database, session_id: int) -> None:
    bridge = _bridge(db, None)
    assert bridge.export_report(session_id, "docx")["error"] == "Format laporan tidak dikenal."  # type: ignore[attr-defined]
    assert "tidak ditemukan" in bridge.export_report(9999, "pdf")["error"]  # type: ignore[attr-defined]
    assert bridge.export_report("abc", "pdf")["ok"] is False  # type: ignore[attr-defined]
    ok = bridge.export_report(session_id, "PDF")  # type: ignore[attr-defined]
    assert ok["ok"] and ok["saved_to"] == ""  # no folder configured: download only


def test_export_report_is_exposed_to_the_ui() -> None:
    from labsentinel_dashboard.app import EXPOSED

    assert "export_report" in EXPOSED
