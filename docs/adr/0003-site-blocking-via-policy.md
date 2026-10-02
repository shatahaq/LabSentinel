# ADR-0003: Pemblokiran situs lewat kebijakan browser, bukan extension

**Status:** Diterima · **Tanggal:** 2026-10-02

## Konteks

Situs tertentu (game online, media sosial, AI chatbot) perlu diblokir di
browser. Extension browser rapuh (bisa dinonaktifkan peserta, perlu
distribusi/izin per-browser) dan menambah permukaan serang.

## Keputusan

Agent (berjalan sebagai SYSTEM) menulis daftar blokir ke **kebijakan Chrome &
Edge di registry** `HKLM\SOFTWARE\Policies\...\URLBlocklist`, dan menonaktifkan
mode incognito/InPrivate lewat `IncognitoModeAvailability` /
`InPrivateModeAvailability` selama pemantauan aktif. Browser menampilkan halaman
"diblokir oleh administrator". **Tanpa extension.**

## Alasan

- Kebijakan via registry tidak bisa dimatikan peserta non-admin.
- Tidak perlu memasang/memelihara extension di tiap browser.

## Konsekuensi & pengaman

- **Jangan menimpa kebijakan institusi**: sebelum menulis, agent mem-backup nilai
  asli, hanya mengelola entri bertanda Lab Sentinel, dan memulihkan nilai asli
  saat dijeda/uninstall/berhenti tidak normal. Penulisan atomik & tahan crash
  (detail di M4).
- **Hanya Chromium (Chrome/Edge)**. Browser lain tidak terpengaruh → **whitelist
  aplikasi tetap pagar utama** (membatasi browser yang boleh berjalan).
- **Pencatatan situs dari judul jendela** hanya pelengkap berakurasi terbatas;
  diberi label "terdeteksi dari judul jendela" dan dideduplikasi. Ini **tidak
  seakurat** extension — keterbatasan yang didokumentasikan secara eksplisit.
