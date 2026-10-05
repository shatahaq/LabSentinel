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

## Pemasangan (pairing) agent

Sudah terbukti otomatis: `tests/unit/test_pairing.py` (kode salah, lockout 5×
per alamat, token per-agent tersimpan sebagai hash, pendaftaran ditutup,
rotasi kode, redaksi log), `tests/integration/test_pairing_flow.py` dan
`test_agent_app_e2e.py` (agent utuh: pairing lewat WebSocket sungguhan →
restart memakai token tersimpan → pairing ulang setelah database dashboard
di-reset; agent mengabaikan proxy sistem).

**Uji manual yang sudah dijalankan (2026-10-05, container Linux, bukan
Windows):** dashboard `--keep-running` + agent sebagai dua proses terpisah,
penemuan lewat UDP broadcast (tanpa `--dashboard`):

| Langkah | Hasil |
|---|---|
| Agent tanpa token & tanpa kode | Pesan "PC ini belum dipasangkan", exit 2 ✅ |
| `--pair` kode salah | Ditolak sekali, pesan "kode pemasangan salah", exit 3, tidak mengunci PC ✅ |
| `--pair` kode benar | "PC ini berhasil dipasangkan", token tersimpan (izin 600), PC muncul **Aktif** di Layar PC ✅ |
| Restart tanpa kode | Masuk dengan token tersimpan, tidak ada pairing ulang ✅ |
| Catatan aktivitas | "Kode pemasangan salah", "PC dipasangkan" tercatat ✅ |
| Layar & ikon tray | Tidak dapat diuji (container tanpa layar; tray "indikator disembunyikan" — benar) |

Ditemukan & diperbaiki saat uji manual: agent sebelumnya mengikuti **proxy
sistem** sehingga koneksi LAN ke dashboard ditolak (HTTP 403) — di sekolah
yang memasang proxy di Windows, agent tidak akan pernah terhubung.

Masih perlu di Windows nyata:

- [ ] 🖥️ Pasang agent di PC lab dengan `labsentinel-agent --pair KODE` → PC
  muncul di "Layar PC" dalam beberapa detik; hitungan "PC sudah terpasang" di
  halaman Aturan bertambah.
- [ ] 🖥️ PC lab yang memakai **proxy di Internet Options** tetap terhubung ke
  dashboard di LAN.
- [ ] 🔑 File token di `C:\ProgramData\LabSentinel\agent\token` **tidak bisa
  dibaca** akun peserta (non-admin): `type` dari akun peserta → "Access is
  denied". (ACL dipasang installer; `chmod` 600 hanya berlaku di POSIX.)
- [ ] Tutup pendaftaran di dashboard → PC baru ditolak dengan pesan
  "pendaftaran PC baru sedang ditutup"; PC lama tetap terhubung.
- [ ] Salah kode 5× dari PC yang sama (jalankan `--pair` lima kali dengan
  kode salah) → percobaan ke-6 dengan kode benar ditolak "tunggu … detik";
  PC lain tetap bisa dipasang.
- [ ] **Clone disk image:** PC yang di-clone *setelah* agent dipasangkan
  berbagi ID & token → keduanya saling memutus. Prosedur yang benar: clone
  sebelum pairing, atau hapus `agent_id` + `token` lalu `--pair` di tiap PC
  hasil clone (lihat panduan deploy).

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

Sudah terbukti otomatis:

- **Laporan** (`tests/unit/test_reports.py`): isi PDF dibaca balik dengan
  pypdf (nama sesi, PC, jenis, catatan uji coba/judul jendela, "Halaman X dari
  Y", gambar bukti tertanam, bukti hilang disebutkan), Excel dibaca balik dengan
  openpyxl (tanggal asli, filter, header beku). Diunduh lewat tombol di browser
  sungguhan terhadap demo lab (PDF & Excel).
- **Anti-tamper** (`tests/integration/test_anti_tamper.py`,
  `tests/unit/test_lifecycle.py`): proses agent **sungguhan** dibunuh SIGKILL →
  start berikutnya melaporkan "Agent dihentikan paksa saat PC menyala" ke
  dashboard; SIGTERM → berhenti bersih, **tidak** dilaporkan; stop layanan
  manual → dilaporkan "dihentikan secara manual"; instans kedua ditolak tanpa
  mengganggu yang berjalan; watchdog menangkap loop macet. Toast merah muncul
  di dashboard sungguhan setelah `kill -9` (diperiksa di browser).

Yang **belum bisa** diverifikasi tanpa Windows + Administrator:

- [ ] Ganti profil (Ujian/Praktikum/Bebas) satu klik → agent menyesuaikan.
- [ ] Laporan PDF terbuka benar di Adobe Reader/Edge; nama PC dan judul jendela
  berhuruf non-Latin tampil (font Segoe UI/Arial dipakai otomatis).
- [ ] Laporan Excel terbuka di Microsoft Excel tanpa peringatan perbaikan;
  kolom tanggal bisa diurutkan dan disaring.
- [ ] 🔑 `labsentinel-agent --pair KODE --pair-only` lalu
  `labsentinel-service install` → layanan **Lab Sentinel Agent** muncul di
  `services.msc`, Startup type *Automatic*, berjalan sebagai *Local System*.
- [ ] 🔑 Tab *Recovery* di `services.msc`: First/Second failure = Restart
  the Service, Subsequent = Restart, "Enable actions for stops with errors"
  tercentang.
- [ ] 🔑 **Kill paksa** `labsentinel-service.exe` dari Task Manager → layanan
  hidup lagi ≤ 10 detik; dashboard menampilkan toast merah "agent dihentikan
  tidak wajar" dan baris di Laporan.
- [ ] 🔑 `net stop LabSentinelAgent` → kebijakan browser dipulihkan
  (`chrome://policy` kosong dari entri Lab Sentinel); `net start` → dashboard
  mencatat "Layanan agent dihentikan secara manual".
- [ ] Restart Windows normal (Start → Restart) → **tidak** ada catatan tamper.
- [ ] Cabut daya / tahan tombol power → setelah menyala, catatan berbunyi "PC
  mati atau restart tanpa shutdown normal".
- [ ] 🔑 Jalankan `labsentinel-agent` manual saat layanan aktif → pesan "Agent
  Lab Sentinel sudah berjalan di PC ini", layanan tidak terganggu.
- [ ] Login sebagai peserta (non-admin) → ikon tray muncul otomatis
  (`HKLM\...\Run\LabSentinelTray`); login sebagai peserta lain → ikon juga
  muncul (log helper di `%LOCALAPPDATA%\LabSentinel\tray.log` masing-masing).
- [ ] 🔑 Hapus nilai `LabSentinelTray` dari kunci Run lalu restart layanan →
  nilainya kembali.
- [ ] 🔑 `icacls C:\ProgramData\LabSentinel\agent\token` hanya menampilkan
  `NT AUTHORITY\SYSTEM` dan `BUILTIN\Administrators`.
- [ ] 🔑 `labsentinel-service remove` → layanan hilang, entri Run hilang,
  kebijakan browser bersih.

### Desktop worker (ADR-0006) — paling penting di Windows

Saat agent berjalan sebagai layanan, **seluruh** penegakan, tangkapan layar,
dan judul jendela bergantung pada desktop worker. Logika dan protokolnya diuji
otomatis (`tests/integration/test_desktop_worker.py`), tetapi pemanggilan
Windows-nya belum pernah dijalankan:

- [ ] 🔑 Setelah login peserta, Task Manager (*Details*, sebagai admin)
  menampilkan `labsentinel-agent.exe` dengan *User name* **SYSTEM** dan
  *Session ID* = sesi peserta (bukan 0). Tidak ada jendela/konsol yang muncul.
- [ ] Kartu PC di dashboard menampilkan **thumbnail layar asli** (bukan hitam)
  dan **tidak** bertanda "layar tidak terjangkau".
- [ ] Dalam mode ditegakkan: buka `notepad.exe` (tidak diizinkan) → peringatan
  muncul lalu Notepad tertutup **dengan sopan** (WM_CLOSE: Notepad menanyakan
  simpan bila ada isi).
- [ ] Buka Steam / launcher game yang jendelanya muncul terlambat → tetap
  ditutup begitu jendelanya tampil.
- [ ] Buka situs terlarang yang lolos kebijakan (judul memuat kata kunci) →
  pelanggaran "Dugaan situs terlarang" dengan tangkapan layar yang benar.
- [ ] Tombol **Kunci layar** di dashboard benar-benar mengunci PC peserta.
- [ ] Peserta (non-admin) mencoba *End task* worker SYSTEM → "Access is denied".
- [ ] 🔑 Admin mengakhiri worker → dalam ≤ 15 detik muncul worker baru; kartu
  sempat menampilkan "layar tidak terjangkau" lalu pulih.
- [ ] Logout lalu login sebagai pengguna lain (atau *Switch user*) → worker
  pindah ke sesi baru; thumbnail menampilkan desktop pengguna baru.
- [ ] Layar dikunci (Win+L) → tidak ada crash; thumbnail hitam/terakhir; setelah
  dibuka, kembali normal.

### Helper tray dinyalakan ulang

- [ ] Peserta menutup ikon tray dari Task Manager → dashboard menandai
  "indikator disembunyikan" → dalam ±30 detik ikon muncul lagi (sebagai akun
  peserta) dan tanda hilang.
- [ ] Setelah login, hanya **satu** ikon Lab Sentinel (Run key dan layanan tidak
  menghasilkan ikon ganda).

## M7 — Packaging & performa

Sudah terbukti di Linux: kedua build PyInstaller berhasil; dashboard beku
melayani UI dan mengekspor PDF/Excel; agent beku pairing (`--pair-only`) lalu
terhubung; tray beku berjalan. Installer Inno Setup **belum pernah
dikompilasi**.

- [ ] `python packaging/build.py --installer` di Windows menghasilkan dua
  installer tanpa error (Inno Setup 6).
- [ ] 🔑 Installer dashboard: pintasan menu Start membuka dashboard dengan
  `--keep-running`; aturan firewall "Lab Sentinel Dashboard (agent)" dan
  "(penemuan)" ada di `wf.msc`; jendela terbuka di Edge bila Chrome tidak ada.
- [ ] 🔑 Installer agent interaktif: kode salah → pesan "Dashboard menolak kode
  ini"; kode benar → PC muncul di dashboard; ikon tray langsung tampil.
- [ ] 🔑 Installer agent senyap: `/VERYSILENT /SUPPRESSMSGBOXES /PAIRCODE=…`
  di 2–3 PC sekaligus → semua muncul; log `%TEMP%\Setup Log*.txt` memuat
  `--pair ... -> 0`.
- [ ] 🔑 Kode dikosongkan (persiapan disk image) → layanan terpasang tetapi
  tidak berjalan, `agent_id` **tidak** ada; clone; di hasil clone setelah boot
  `agent_id` dibuat; `--pair-only` + `sc start` → muncul di dashboard dengan ID
  berbeda per PC.
- [ ] 🔑 Upgrade (jalankan installer versi sama/baru di atas yang lama) →
  pairing dipertahankan, **tidak** ada catatan "dihentikan secara manual".
- [ ] 🔑 Uninstall agent → layanan, aturan firewall, entri Run, dan berkas
  `token` hilang; `chrome://policy` bersih.
- [ ] Agent berjalan dari folder `C:\Program Files\LabSentinel\Agent` tanpa
  Python terpasang di PC.
- [ ] **Performa agent:** CPU rata-rata < 2% dan RAM < 100 MB saat idle
  (ukur dengan Performance Monitor / `Get-Counter`) — **jumlahkan layanan +
  desktop worker + helper tray**. Catat angkanya.
