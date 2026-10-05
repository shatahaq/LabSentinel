"""Session report as an Excel workbook (openpyxl).

For people who want to sort, filter or merge it with a grade sheet: one row per
record, real date cells (not text), frozen headers and filters on every sheet.
"""

from __future__ import annotations

import io
from datetime import datetime
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from labsentinel_dashboard.reports.model import (
    SessionReport,
    action_label,
    fmt_duration,
    kind_label,
    mode_label,
)

DATE_FMT = "dd/mm/yyyy hh:mm:ss"
HEADER_FONT = Font(bold=True, color="FFFFFF")
HEADER_FILL = PatternFill("solid", fgColor="2F5FD0")
HEADER_BORDER = Border(bottom=Side(style="thin", color="1E47A8"))


def _dt(ts: float | None) -> datetime | None:
    # Excel has no time zones; write the dashboard's local wall-clock time.
    return datetime.fromtimestamp(ts) if ts else None


def _sheet(ws: Worksheet, headers: list[str], rows: list[list[Any]], widths: list[int]) -> None:
    ws.append(headers)
    for cell in ws[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.border = HEADER_BORDER
        cell.alignment = Alignment(vertical="center")
    for row in rows:
        ws.append(row)
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, datetime):
                cell.number_format = DATE_FMT
            cell.alignment = Alignment(vertical="top", wrap_text=isinstance(cell.value, str))
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions


def render_xlsx(report: SessionReport) -> bytes:
    s = report.session
    wb = Workbook()

    # ---- Ringkasan
    ws = wb.active
    assert ws is not None
    ws.title = "Ringkasan"
    summary: list[tuple[str, Any]] = [
        ("Nama sesi", s.name),
        ("Mode", mode_label(s.mode)),
        ("Mulai", _dt(report.window_start)),
        ("Selesai", _dt(s.ended_at) if s.ended_at else "masih berjalan"),
        ("Durasi", fmt_duration(report.duration_s)),
        ("Laporan dibuat", _dt(report.generated_at)),
        ("Total pelanggaran", report.total),
        ("Saat aturan ditegakkan", report.total - report.dry_run_count),
        ("Hanya dicatat (uji coba / Bebas)", report.dry_run_count),
        ("Aplikasi ditutup otomatis", report.totals_by_kind.get("app_closed", 0)),
        ("Dikenali dari judul jendela (bisa keliru)", report.title_based_count),
        ("Agent dihentikan tidak wajar", report.tamper_count),
        ("PC dengan pelanggaran", len(report.pcs)),
        ("PC terdaftar", report.registered_pcs),
    ]
    ws.append(["Lab Sentinel - Laporan sesi"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    for label, value in summary:
        ws.append([label, value])
    for row in ws.iter_rows(min_row=3, max_col=1):
        row[0].font = Font(bold=True)
    for row in ws.iter_rows(min_row=3, min_col=2, max_col=2):
        cell = row[0]
        if isinstance(cell.value, datetime):
            cell.number_format = DATE_FMT
        cell.alignment = Alignment(horizontal="left")
    ws.append([])
    ws.append(["Per jenis", "Jumlah"])
    header_row = ws.max_row
    for cell in ws[header_row]:
        cell.font = Font(bold=True)
    for kind, n in sorted(report.totals_by_kind.items(), key=lambda kv: -kv[1]):
        ws.append([kind_label(kind), n])
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 30

    # ---- Per PC
    _sheet(
        wb.create_sheet("Per PC"),
        ["PC", "ID agent", "Pengguna", "Jumlah", "Saat ditegakkan", "Hanya dicatat",
         *[kind_label(k) for k in sorted(report.totals_by_kind)], "Pertama", "Terakhir"],
        [
            [pc.hostname, pc.agent_id, pc.username, pc.total, pc.enforced,
             pc.total - pc.enforced,
             *[pc.by_kind.get(k, 0) for k in sorted(report.totals_by_kind)],
             _dt(pc.first_at), _dt(pc.last_at)]
            for pc in report.pcs
        ],
        [16, 26, 16, 9, 16, 14, *[18] * len(report.totals_by_kind), 20, 20],
    )

    # ---- Pelanggaran
    _sheet(
        wb.create_sheet("Pelanggaran"),
        ["Waktu", "PC", "ID agent", "Jenis", "Aplikasi / situs", "Keterangan",
         "Dari judul jendela", "Uji coba", "Ada bukti"],
        [
            [_dt(v.occurred_at), v.hostname, v.agent_id, kind_label(v.kind), v.subject,
             v.detail, "ya" if v.detected_from_title else "tidak",
             "ya" if v.dry_run else "tidak", "ya" if v.screenshot_path else "tidak"]
            for v in report.violations
        ],
        [20, 16, 26, 24, 30, 50, 12, 10, 10],
    )

    # ---- Catatan aktivitas
    _sheet(
        wb.create_sheet("Catatan aktivitas"),
        ["Waktu", "Oleh", "Tindakan", "Sasaran", "Hasil"],
        [[_dt(a.ts), a.actor, action_label(a.action), a.target, a.result]
         for a in report.audit],
        [20, 14, 40, 26, 40],
    )

    wb.properties.title = f"Laporan sesi - {s.name}"
    wb.properties.creator = "Lab Sentinel"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
