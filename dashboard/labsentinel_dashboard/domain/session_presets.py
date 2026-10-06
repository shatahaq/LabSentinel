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
        "apps": "Hanya Chrome dan Edge",
        "sites": "Semua kategori dan situs tambahan diblokir",
        "description": (
            "Hanya Chrome dan Edge yang boleh berjalan; aplikasi untuk Praktikum "
            "tidak berlaku. Semua kategori situs dan situs tambahan diblokir."
        ),
    },
    "lab": {
        "label": "Praktikum",
        "summary": "Sedang",
        "apps": "Browser dan aplikasi yang diizinkan",
        "sites": "Kategori pilihan dan situs tambahan diblokir",
        "description": (
            "Browser ditambah aplikasi yang Anda izinkan, misalnya VS Code atau "
            "XAMPP. Kategori situs yang Anda pilih dan situs tambahan diblokir."
        ),
    },
    "free": {
        "label": "Bebas",
        "summary": "Hanya memantau",
        "apps": "Tidak ada yang ditutup",
        "sites": "Tidak ada yang diblokir",
        "description": (
            "Tidak ada aplikasi yang ditutup dan tidak ada situs yang diblokir. "
            "Layar tetap terlihat, dan aplikasi di luar daftar tetap dicatat."
        ),
    },
}
