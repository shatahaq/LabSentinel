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

Sudah terbukti otomatis (lihat `tests/integration/test_m3_control.py`,
`test_tray_bridge.py`, `test_tray_end_to_end.py`): alur jeda→ACK→ikon berubah,
audit log, helper tanpa wewenang, dan deteksi indikator hilang. Yang di bawah
ini memverifikasi tampilannya di desktop nyata.

Demo manual ikon tray (dua terminal):

```powershell
python scripts/tray_demo.py
python -m labsentinel_agent.tray --data-dir "<dir yang dicetak perintah di atas>"
```

- [ ] Ikon tray muncul; **arahkan kursor** → tooltip menyebut status dengan
  kata-kata ("Pemantauan aktif"), bukan hanya warna.
- [ ] Status berganti Aktif → Dijeda → Uji coba → Terputus; warna **dan** glyph
  ikut berubah tiap status.
- [ ] Klik kanan ikon → menu hanya menampilkan status (read-only); **tidak ada**
  opsi untuk menjeda/mematikan pemantauan.
- [ ] Peringatan "aplikasi akan ditutup" muncul sebagai balloon dan **tidak
  merebut fokus** — ketik di aplikasi lain saat popup muncul, ketikan tidak
  terpotong.
- [ ] Admin menekan **Jeda** di dashboard → ikon tray peserta berubah
  "Dijeda" dalam hitungan detik; aplikasi di luar whitelist **tidak** lagi
  ditutup.
- [ ] Admin **Lanjut** → enforcement aktif lagi dalam hitungan detik, tanpa
  restart PC.
- [ ] 🔑 Peserta **tidak** dapat menjeda sendiri: matikan proses helper tray dari
  Task Manager → penegakan aturan **tetap berjalan** (buka aplikasi terlarang,
  tetap ditutup) dan dashboard menandai indikator tidak terlihat.
- [ ] Setiap aksi tercatat di **audit log** (siapa, kapan, PC mana).

## M4 — Kebijakan situs browser (🔑)

Logika backup/restore sudah terbukti otomatis terhadap registry tiruan
(`tests/unit/test_browser_policy.py`, 22 test), termasuk kebijakan institusi
selamat, pemulihan setelah crash, dan penolakan backup rusak. **Yang belum bisa
diverifikasi tanpa hak admin: penulisan `HKLM` sungguhan dan reaksi Chrome/Edge
asli.** Semua item di bawah ini wajib dijalankan di PC lab sebagai Administrator.

> ⚠️ Jalankan dulu di PC uji, bukan PC produksi. Ambil cadangan registry
> (`reg export HKLM\SOFTWARE\Policies policies-backup.reg`) sebelum mulai.

- [ ] 🔑 Buka situs terlarang di Chrome → halaman **"diblokir oleh
  administrator"**. Ulangi di Edge.
- [ ] 🔑 Verifikasi entri benar-benar terpasang di `chrome://policy` dan
  `edge://policy` (cari `URLBlocklist`), lalu klik **Reload policies**.
- [ ] 🔑 Mode incognito/InPrivate tidak bisa dibuka selama pemantauan aktif
  (`IncognitoModeAvailability` / `InPrivateModeAvailability` = 1).
- [ ] 🔑 **Kebijakan institusi tidak rusak** — uji paling penting:
  1. Pasang dulu entri institusi sendiri di
     `HKLM\SOFTWARE\Policies\Google\Chrome\URLBlocklist` (mis. `1` =
     `malware.example`) dan satu kebijakan lain (mis. `HomepageLocation`).
  2. Jalankan agent dengan pemantauan aktif → entri institusi **masih ada** dan
     tetap berada di urutan awal; entri Lab Sentinel menyusul sesudahnya.
  3. Jeda pemantauan → entri Lab Sentinel hilang, entri institusi **utuh**,
     `HomepageLocation` tidak tersentuh.
  4. Lanjutkan → terpasang lagi.
- [ ] 🔑 **Pemulihan setelah mati paksa:** saat pemantauan aktif, matikan paksa
  proses agent (bukan stop service). Jalankan ulang → agent memulihkan kebijakan
  asli lebih dulu (lihat log `found leftover policy backup`), lalu memasang lagi.
- [ ] 🔑 **Uninstall** → seluruh entri Lab Sentinel hilang, entri institusi utuh,
  dan `HKLM\SOFTWARE\LabSentinel\PolicyState\Managing` = 0.
- [ ] Tanpa hak admin → agent **tidak crash**, hanya mencatat error "access
  denied"; penegakan whitelist aplikasi tetap berjalan.
- [ ] Judul jendela browser yang memuat kata kunci terlarang → pelanggaran
  tercatat dengan label **"terdeteksi dari judul jendela"**, dan judul yang sama
  tidak dilaporkan berulang (dedup).
- [ ] **Uji false positive:** buka Google Classroom, Google Drive, dan materi
  kuliah biasa → **tidak** muncul pelanggaran situs.
- [ ] Browser selain Chrome/Edge (mis. Firefox) **tidak** terblokir situsnya —
  konfirmasi bahwa whitelist aplikasi yang menahannya (Firefox ditutup karena
  tidak ada di whitelist).

## M5 — Streaming & UI dashboard

Sudah terbukti otomatis: gating streaming (tidak menangkap layar saat dijeda
atau tak ada penonton), dan UI dijalankan sungguhan lewat `scripts/demo_lab.py`
dengan 14 PC simulasi (thumbnail live terkonfirmasi 14/14).

- [ ] 🖥️ Grid menampilkan thumbnail untuk semua PC lab; buka satu PC → gambar
  jelas dan lebih mulus. Tutup → kembali ke thumbnail.
- [ ] **Privasi:** jeda pemantauan → thumbnail berhenti diperbarui dan agent
  **berhenti menangkap layar** (periksa CPU agent turun). Pindah ke halaman
  selain "Layar PC" → streaming juga berhenti.
- [ ] Beban jaringan wajar dengan 40 PC terbuka di grid (pantau switch/Wi-Fi).
- [ ] **Lazy render:** gulir grid 40+ PC → tetap mulus, tidak tersendat.
- [ ] **Aksesibilitas:** jalankan dengan layar hitam-putih (atau simulasi buta
  warna) → status tiap PC masih bisa dibedakan dari ikon dan teksnya.
- [ ] **Resolusi lab:** buka pada 1366×768 → sidebar menyusut jadi ikon, tidak
  ada gulir horizontal, semua tombol tetap terjangkau.
- [ ] Mode terang dan gelap sama-sama terbaca di proyektor lab.
- [ ] Navigasi keyboard: Tab menjangkau semua kontrol, Esc menutup dialog,
  angka 1–5 berpindah halaman.
- [ ] Tutup jendela dashboard saat `--keep-running` → pencatatan pelanggaran
  **tetap berjalan**; buka lagi di `http://localhost:8080` → data utuh.

## M6 — Mode, laporan, anti-tamper

- [ ] Ganti profil (Ujian/Praktikum/Bebas) satu klik → agent menyesuaikan.
- [ ] Ekspor laporan PDF & Excel berisi riwayat pelanggaran per sesi.
- [ ] 🔑 Kill paksa proses agent (Task Manager) → watchdog/Service
  menghidupkannya lagi; dashboard mencatat **pelanggaran tamper**.

## M7 — Packaging & performa

- [ ] Installer memasang agent sebagai Windows Service dan berjalan saat boot.
- [ ] **Performa agent:** CPU rata-rata < 2% dan RAM < 100 MB saat idle
  (ukur dengan Performance Monitor / `Get-Counter`). Catat angkanya.
