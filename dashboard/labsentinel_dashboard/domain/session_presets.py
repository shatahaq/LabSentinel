"""Human-facing labels for the enforcement profiles.

Written for lecturers and lab staff, not engineers: each profile says plainly
what it allows and what it blocks, so the right one can be chosen without
reading a manual.
"""

from __future__ import annotations

PROFILE_LABELS: dict[str, dict[str, str]] = {
    "exam": {
        "label": "Ujian",
        "summary": "Paling ketat",
        "apps": "Hanya browser",
        "sites": "Semua kategori diblokir",
        "description": (
            "Hanya Chrome dan Edge yang boleh berjalan. Game, media sosial, "
            "AI chatbot, situs jawaban, dan hiburan diblokir."
        ),
    },
    "lab": {
        "label": "Praktikum",
        "summary": "Sedang",
        "apps": "Browser + aplikasi mata kuliah",
        "sites": "Game, media sosial, dan AI chatbot diblokir",
        "description": (
            "Browser ditambah aplikasi yang Anda izinkan, misalnya VS Code atau "
            "XAMPP. Situs yang paling mengganggu tetap diblokir."
        ),
    },
    "free": {
        "label": "Bebas",
        "summary": "Tanpa pembatasan",
        "apps": "Semua aplikasi",
        "sites": "Tidak ada yang diblokir",
        "description": (
            "Tidak ada aplikasi yang ditutup dan tidak ada situs yang diblokir. "
            "Pemantauan layar tetap berjalan agar Anda bisa mengawasi."
        ),
    },
}
