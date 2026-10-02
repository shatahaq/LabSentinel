# Checklist Uji Manual (butuh Windows nyata + hak admin)

Hal-hal yang **tidak** dapat diverifikasi dalam pengembangan biasa (CI/loopback)
dan harus diuji di PC Windows nyata, sebagian sebagai Administrator. Tandai
`[x]` bila lulus, catat versi OS/browser.

Legenda: 🔑 = butuh Administrator, 🖥️ = butuh dua PC (agent + dashboard).

## M1 — Transport (dapat diuji sekarang)

- [ ] 🖥️ Agent di PC lab menemukan dashboard **tanpa konfigurasi IP** (UDP
  broadcast) dan terhubung. *Catatan: broadcast lintas-subnet/VLAN atau yang
  diblokir switch dapat gagal; sediakan `direct_url` sebagai cadangan.*
- [ ] 🖥️ Heartbeat muncul di dashboard; cabut kabel LAN agent → status menjadi
  `dropped` setelah timeout; sambung lagi → `online` kembali (auto-reconnect).
- [ ] Token salah → agent ditolak (`reject`), tidak muncul di dashboard.

## M2 — Enforcement whitelist (sebagian 🔑)

- [ ] Buka game/aplikasi di luar whitelist → muncul peringatan, lalu aplikasi
  ditutup setelah grace period.
- [ ] Rename file game menjadi `chrome.exe` di folder lain → **tetap terdeteksi**
  (hash/path tidak cocok) dan ditutup.
- [ ] **Uji keselamatan:** jalankan agent, pastikan proses sistem (`csrss.exe`,
  `winlogon.exe`, `svchost.exe`, Defender, dll.) **tidak pernah** ditutup —
  verifikasi lewat Event Viewer / log agent.
- [ ] Aplikasi Microsoft Store (host `ApplicationFrameHost.exe`) ditangani benar
  (tidak menutup host; identifikasi app sebenarnya).
- [ ] **Dry-run (default install):** aplikasi terlarang hanya dicatat, **tidak**
  ditutup.

## M3 — Jeda/lanjut & tray

- [ ] Admin menekan **Jeda** di dashboard → ikon tray peserta berubah
  "Dijeda"; aplikasi di luar whitelist **tidak** lagi ditutup.
- [ ] Admin **Lanjut** → enforcement aktif lagi dalam hitungan detik, tanpa
  restart PC.
- [ ] Peserta **tidak** dapat menjeda sendiri.
- [ ] Setiap aksi tercatat di **audit log** (siapa, kapan, PC mana).

## M4 — Kebijakan situs browser (🔑)

- [ ] Buka situs terlarang di Chrome → halaman **"diblokir oleh administrator"**.
  Verifikasi entri di `chrome://policy` dan `edge://policy`.
- [ ] Mode incognito/InPrivate dinonaktifkan selama pemantauan aktif.
- [ ] **Kebijakan institusi tidak rusak:** set kebijakan `URLBlocklist` institusi
  lebih dulu → jalankan agent → pastikan entri institusi tetap ada setelah agent
  menulis & setelah agent berhenti/di-uninstall (restore benar).
- [ ] Jeda → kebijakan Lab Sentinel dilepas (situs tak lagi diblokir) tanpa
  menghapus entri institusi; Lanjut → dipasang lagi.
- [ ] Judul jendela browser yang memuat kata kunci terlarang → pelanggaran
  tercatat dengan label "terdeteksi dari judul jendela", tidak spam (dedup).

## M5 — Streaming & grid

- [ ] Grid menampilkan thumbnail ~1 fps untuk banyak PC; buka satu PC → resolusi
  lebih tinggi. Jeda / tidak ada yang melihat → streaming **berhenti total**.

## M6 — Mode, laporan, anti-tamper

- [ ] Ganti profil (Ujian/Praktikum/Bebas) satu klik → agent menyesuaikan.
- [ ] Ekspor laporan PDF & Excel berisi riwayat pelanggaran per sesi.
- [ ] 🔑 Kill paksa proses agent (Task Manager) → watchdog/Service
  menghidupkannya lagi; dashboard mencatat **pelanggaran tamper**.

## M7 — Packaging & performa

- [ ] Installer memasang agent sebagai Windows Service dan berjalan saat boot.
- [ ] **Performa agent:** CPU rata-rata < 2% dan RAM < 100 MB saat idle
  (ukur dengan Performance Monitor / `Get-Counter`). Catat angkanya.
