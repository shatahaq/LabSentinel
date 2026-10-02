# ADR-0002: Whitelist aplikasi, bukan blacklist

**Status:** Diterima · **Tanggal:** 2026-10-02

## Konteks

Peserta dapat membuka game, aplikasi portable, atau software baru yang belum
pernah terdaftar. Pendekatan blacklist harus mendaftarkan setiap hal terlarang
lebih dulu sehingga mudah diakali.

## Keputusan

Agent menegakkan **whitelist**: hanya aplikasi yang diizinkan yang boleh
berjalan; selain itu ditutup secara bertahap (graceful). Default whitelist yang
tidak bisa dihapus: `chrome.exe`, `msedge.exe`, `explorer.exe`, dan biner agent
sendiri.

## Alasan

- Lebih kuat: aplikasi tak dikenal otomatis tertutup tanpa perlu didaftarkan.
- Lebih sederhana dikelola untuk skenario ujian/praktikum yang set aplikasinya
  sempit.

## Konsekuensi & pengaman

- **Risiko menutup proses sistem** → ditangani dengan daftar proteksi
  *hardcoded* (`PROTECTED_PROCESSES`) yang tidak dapat dihapus lewat konfigurasi,
  plus syarat: hanya proses **berjendela terlihat** milik **pengguna yang
  login** yang boleh dievaluasi (detail di M2).
- **Risiko false positive** → mode **dry-run** (audit saja) menjadi default saat
  instalasi pertama, agar admin menguji aturan tanpa menutup apa pun.
- Verifikasi berlapis (nama + path + SHA-256) mencegah game yang di-rename
  menjadi `chrome.exe` lolos.
