"""Session report as a printable PDF (ReportLab).

Laid out for A4 and for being read by someone who was not in the room: what
happened, on which PC, and how sure we are. Low-precision detections (window
titles) and dry-run records are labelled as such rather than mixed in.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (
    CondPageBreak,
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from labsentinel_dashboard.reports.model import (
    SessionReport,
    action_label,
    fmt_datetime,
    fmt_duration,
    fmt_time,
    kind_label,
    mode_label,
)

log = logging.getLogger("labsentinel.dashboard.reports")

# Same restrained palette as the dashboard (tokens.css, light theme).
INK = colors.HexColor("#15171c")
INK_2 = colors.HexColor("#596070")
INK_3 = colors.HexColor("#868d9b")
RULE = colors.HexColor("#e2e5ea")
SUBTLE = colors.HexColor("#f5f6f8")
ACCENT = colors.HexColor("#2f5fd0")
WARN = colors.HexColor("#8a5a00")
DANGER = colors.HexColor("#b3261e")

DEFAULT_MAX_EVIDENCE = 30

# A TrueType font covers window titles in any script; the built-in Helvetica
# only covers Latin-1. Tried in order; Helvetica is the last resort.
_FONT_CANDIDATES: list[tuple[str, str]] = [
    ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/segoeuib.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/TTF/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"),
]
_fonts: tuple[str, str] | None = None


def _register_fonts() -> tuple[str, str]:
    global _fonts
    if _fonts is not None:
        return _fonts
    for regular, bold in _FONT_CANDIDATES:
        if Path(regular).is_file() and Path(bold).is_file():
            try:
                pdfmetrics.registerFont(TTFont("LS-Regular", regular))
                pdfmetrics.registerFont(TTFont("LS-Bold", bold))
                _fonts = ("LS-Regular", "LS-Bold")
                return _fonts
            except Exception:
                log.debug("could not register %s", regular, exc_info=True)
    _fonts = ("Helvetica", "Helvetica-Bold")
    return _fonts


def _styles() -> dict[str, ParagraphStyle]:
    regular, bold = _register_fonts()
    base = ParagraphStyle("base", fontName=regular, fontSize=9, leading=12, textColor=INK,
                          alignment=TA_LEFT)
    return {
        "base": base,
        "small": ParagraphStyle("small", parent=base, fontSize=8, leading=10.5, textColor=INK_2),
        "muted": ParagraphStyle("muted", parent=base, fontSize=8, leading=10.5, textColor=INK_3),
        "cell": ParagraphStyle("cell", parent=base, fontSize=8, leading=10),
        "cellb": ParagraphStyle("cellb", parent=base, fontName=bold, fontSize=8, leading=10),
        "th": ParagraphStyle("th", parent=base, fontName=bold, fontSize=7.5, leading=9.5,
                             textColor=INK_2),
        "eyebrow": ParagraphStyle("eyebrow", parent=base, fontName=bold, fontSize=8,
                                  textColor=ACCENT, spaceAfter=2),
        "title": ParagraphStyle("title", parent=base, fontName=bold, fontSize=18, leading=22),
        "h2": ParagraphStyle("h2", parent=base, fontName=bold, fontSize=11.5, leading=14,
                             spaceBefore=12, spaceAfter=6),
        "kpi": ParagraphStyle("kpi", parent=base, fontName=bold, fontSize=17, leading=20),
        "kpil": ParagraphStyle("kpil", parent=base, fontSize=7.5, leading=9.5, textColor=INK_2),
    }


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(text), style)


def _table(rows: list[list[Any]], widths: list[float], *, zebra: bool = True) -> Table:
    t = Table(rows, colWidths=widths, repeatRows=1)
    style: list[Any] = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK_3),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    if zebra:
        for i in range(2, len(rows), 2):
            style.append(("BACKGROUND", (0, i), (-1, i), SUBTLE))
    t.setStyle(TableStyle(style))
    return t


class _NumberedCanvas(rl_canvas.Canvas):  # type: ignore[misc]
    """Adds "Halaman X dari Y" once the total page count is known."""

    footer_left = ""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._saved: list[dict[str, Any]] = []

    def showPage(self) -> None:  # ReportLab's method name
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self) -> None:
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self._footer(total)
            super().showPage()
        super().save()

    def _footer(self, total: int) -> None:
        regular, _ = _register_fonts()
        width, _height = A4
        self.setFont(regular, 7.5)
        self.setFillColor(INK_3)
        self.setStrokeColor(RULE)
        self.setLineWidth(0.4)
        self.line(18 * mm, 13 * mm, width - 18 * mm, 13 * mm)
        self.drawString(18 * mm, 9 * mm, self.footer_left)
        self.drawRightString(
            width - 18 * mm, 9 * mm, f"Halaman {self._pageNumber} dari {total}"
        )


def render_pdf(report: SessionReport, *, max_evidence: int = DEFAULT_MAX_EVIDENCE) -> bytes:
    st = _styles()
    s = report.session
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=20 * mm,
        title=f"Laporan sesi - {s.name}", author="Lab Sentinel",
        subject="Laporan pelanggaran per sesi",
    )
    full = doc.width
    story: list[Any] = []

    # ---- heading
    story.append(_p("LAB SENTINEL  ·  LAPORAN SESI", st["eyebrow"]))
    story.append(_p(s.name, st["title"]))
    story.append(Spacer(1, 6))
    status = "masih berjalan saat laporan dibuat" if report.still_running else (
        {"ended": "selesai", "cancelled": "dibatalkan"}.get(s.status, s.status)
    )
    meta = [
        ("Mode", mode_label(s.mode)),
        ("Mulai", fmt_datetime(report.window_start)),
        ("Selesai", fmt_datetime(s.ended_at) if s.ended_at else "-"),
        ("Durasi", fmt_duration(report.duration_s)),
        ("Status", status),
        ("Dibuat", fmt_datetime(report.generated_at)),
    ]
    meta_rows = [
        [_p(meta[i][0], st["small"]), _p(meta[i][1], st["cell"]),
         _p(meta[i + 3][0], st["small"]), _p(meta[i + 3][1], st["cell"])]
        for i in range(3)
    ]
    mt = Table(meta_rows, colWidths=[22 * mm, full / 2 - 22 * mm, 22 * mm, full / 2 - 22 * mm])
    mt.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
    ]))
    story.append(mt)
    story.append(Spacer(1, 10))

    # ---- key figures
    kpis = [
        (str(report.total), "pelanggaran tercatat"),
        (str(len(report.pcs)), f"PC dengan pelanggaran (dari {report.registered_pcs} terdaftar)"),
        (str(report.totals_by_kind.get("app_closed", 0)), "aplikasi ditutup otomatis"),
        (str(report.tamper_count), "agent dihentikan tidak wajar"),
    ]
    kt = Table(
        [[_p(v, st["kpi"]) for v, _ in kpis], [_p(label, st["kpil"]) for _, label in kpis]],
        colWidths=[full / 4] * 4,
    )
    kt.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, RULE),
        ("LINEBEFORE", (1, 0), (-1, -1), 0.6, RULE),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, 0), 7),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 7), ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TEXTCOLOR", (3, 0), (3, 0), DANGER if report.tamper_count else INK),
    ]))
    story.append(kt)

    notes: list[str] = []
    if report.dry_run_count:
        notes.append(
            f"{report.dry_run_count} catatan terjadi saat mode uji coba: hanya dicatat, "
            "aplikasi tidak ditutup dan situs tidak diblokir."
        )
    if report.title_based_count:
        notes.append(
            f"{report.title_based_count} catatan 'Dugaan situs terlarang' dikenali dari judul "
            "jendela browser dan bisa keliru; periksa bukti tangkapan layarnya sebelum "
            "mengambil tindakan."
        )
    if report.still_running:
        notes.append("Sesi masih berjalan; angka di laporan ini dapat bertambah.")
    for note in notes:
        story.append(Spacer(1, 4))
        story.append(_p(note, st["small"]))

    # ---- by kind
    if report.totals_by_kind:
        story.append(_p("Ringkasan per jenis", st["h2"]))
        rows: list[list[Any]] = [[_p("Jenis", st["th"]), _p("Jumlah", st["th"])]]
        for kind, n in sorted(report.totals_by_kind.items(), key=lambda kv: -kv[1]):
            rows.append([_p(kind_label(kind), st["cell"]), _p(str(n), st["cell"])])
        story.append(_table(rows, [full - 30 * mm, 30 * mm]))

    # ---- per PC
    story.append(_p("Per PC", st["h2"]))
    if not report.pcs:
        story.append(_p("Tidak ada pelanggaran tercatat pada sesi ini.", st["base"]))
    else:
        pc_heads = (
            "PC", "Pengguna", "Jumlah", "Uji coba", "Jenis terbanyak", "Pertama", "Terakhir",
        )
        rows = [[_p(h, st["th"]) for h in pc_heads]]
        for pc in report.pcs:
            top = max(pc.by_kind.items(), key=lambda kv: kv[1])[0]
            rows.append([
                _p(pc.hostname, st["cellb"]), _p(pc.username or "-", st["cell"]),
                _p(str(pc.total), st["cell"]), _p(str(pc.total - pc.enforced), st["cell"]),
                _p(kind_label(top), st["cell"]),
                _p(fmt_time(pc.first_at), st["cell"]), _p(fmt_time(pc.last_at), st["cell"]),
            ])
        story.append(_table(rows, [30 * mm, 26 * mm, 15 * mm, 16 * mm,
                                   full - 135 * mm, 24 * mm, 24 * mm]))

    # ---- chronological list
    if report.violations:
        story.append(CondPageBreak(40 * mm))
        story.append(_p("Daftar pelanggaran", st["h2"]))
        heads = ("Waktu", "PC", "Jenis", "Aplikasi / situs", "Keterangan")
        rows = [[_p(h, st["th"]) for h in heads]]
        for v in report.violations:
            jenis = kind_label(v.kind)
            if v.dry_run:
                jenis += " (uji coba)"
            rows.append([
                _p(fmt_time(v.occurred_at), st["cell"]), _p(v.hostname or v.agent_id, st["cell"]),
                _p(jenis, st["cell"]), _p(v.subject, st["cellb"]), _p(v.detail, st["small"]),
            ])
        story.append(_table(rows, [17 * mm, 26 * mm, 34 * mm, 38 * mm, full - 115 * mm]))

    # ---- evidence
    with_shots = [v for v in report.violations if v.screenshot_path]
    if with_shots:
        story.append(CondPageBreak(70 * mm))
        story.append(_p("Bukti tangkapan layar", st["h2"]))
        shown = with_shots[:max_evidence]
        if len(with_shots) > len(shown):
            story.append(_p(
                f"Menampilkan {len(shown)} dari {len(with_shots)} bukti. Bukti lainnya dapat "
                "dibuka di halaman Laporan pada dashboard.", st["small"]))
            story.append(Spacer(1, 4))
        cells = [_evidence_cell(v, st, (full - 6 * mm) / 2) for v in shown]
        grid = [cells[i:i + 2] + ([""] if len(cells[i:i + 2]) == 1 else [])
                for i in range(0, len(cells), 2)]
        gt = Table(grid, colWidths=[full / 2] * 2)
        gt.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ]))
        story.append(gt)

    # ---- supervisor actions
    story.append(CondPageBreak(30 * mm))
    story.append(_p("Tindakan selama sesi", st["h2"]))
    if not report.audit:
        story.append(_p("Tidak ada tindakan tercatat.", st["base"]))
    else:
        rows = [[_p(h, st["th"]) for h in ("Waktu", "Oleh", "Tindakan", "Sasaran", "Hasil")]]
        for a in report.audit:
            rows.append([
                _p(fmt_time(a.ts), st["cell"]), _p(a.actor, st["cell"]),
                _p(action_label(a.action), st["cell"]), _p(a.target or "-", st["cell"]),
                _p(a.result or "-", st["small"]),
            ])
        story.append(_table(rows, [17 * mm, 22 * mm, 55 * mm, 40 * mm, full - 134 * mm]))

    story.append(Spacer(1, 14))
    story.append(_p(
        "Lab Sentinel hanya mencatat aplikasi yang dibuka, judul jendela, dan tangkapan layar "
        "saat terjadi pelanggaran. Ketikan keyboard, suara, dan kamera tidak pernah direkam. "
        "Peserta melihat indikator pemantauan selama sesi berlangsung.", st["muted"]))

    canvas_cls = type("ReportCanvas", (_NumberedCanvas,),
                      {"footer_left": f"Lab Sentinel · {s.name}"[:90]})
    doc.build(story, canvasmaker=canvas_cls)
    return buf.getvalue()


def _evidence_cell(v: Any, st: dict[str, ParagraphStyle], width: float) -> Any:
    caption = [
        _p(f"{fmt_time(v.occurred_at)} · {v.hostname or v.agent_id}", st["cellb"]),
        _p(f"{kind_label(v.kind)}: {v.subject}", st["small"]),
    ]
    try:
        from PIL import Image as PILImage

        path = Path(v.screenshot_path)
        with PILImage.open(path) as im:
            w, h = im.size
        draw_w, draw_h = width, (width * h / w if w else width * 0.5625)
        if draw_h > width:  # portrait screen: fit the height, keep proportions
            draw_w, draw_h = draw_w * width / draw_h, width
        image: Any = Image(str(path), width=draw_w, height=draw_h)
    except Exception:
        image = _p("Berkas bukti tidak ditemukan.", st["muted"])
    # A table cell takes a list of flowables; each grid row stays on one page.
    return [image, Spacer(1, 2), *caption]
