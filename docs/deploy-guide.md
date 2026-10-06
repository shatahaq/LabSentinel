# Panduan Deploy Lab

Langkah memasang Lab Sentinel di satu lab komputer: satu PC pengawas
(dashboard) dan PC-PC peserta (agent). Untuk pemakaian sehari-hari lihat
[admin-guide.md](admin-guide.md); batasan sistem ada di
[limitations.md](limitations.md).

## 1. Persyaratan

| | PC pengawas | PC peserta |
|---|---|---|
| OS | Windows 10/11 64-bit | Windows 10/11 64-bit |
| Hak saat memasang | Administrator | Administrator |
| Akun saat dipakai | bebas | **akun peserta non-administrator** (wajib) |
| Browser | Chrome atau Edge (untuk jendela dashboard) | Chrome dan/atau Edge |
| Ruang disk | ±200 MB + data (bukti tangkapan layar) | ±150 MB |

Berkas installer (dibuat dengan `python packaging/build.py --installer`, lihat
§8):

- `LabSentinelDashboard-Setup-<versi>.exe`
- `LabSentinelAgent-Setup-<versi>.exe`

## 2. Jaringan

- Pengawas dan peserta sebaiknya di **satu segmen jaringan (broadcast domain)**:
  agent menemukan dashboard lewat broadcast UDP, tanpa mengatur IP.
- **Beri IP tetap** (atau reservasi DHCP) pada PC pengawas.
- Port yang dipakai (installer membuat aturan firewall Windows-nya):

| Port | Arah | Untuk |
|---|---|---|
| TCP 8765 | peserta → pengawas | koneksi agent |
| UDP 50123 | dua arah (broadcast) | penemuan dashboard |
| TCP 8080 | hanya `localhost` di PC pengawas | UI dashboard (tidak terbuka ke jaringan) |

- Bila broadcast diblokir switch, atau peserta di VLAN/subnet lain, isi
  **alamat dashboard** (`ws://IP-PENGAWAS:8765`) saat memasang agent.
- Lalu lintas agent tidak terenkripsi (lihat batasan); utamakan VLAN lab
  tersendiri.

### Antivirus dan SmartScreen (lakukan sebelum memasang)

Agent bekerja seperti software pengawas pada umumnya: berjalan sebagai service,
mengambil tangkapan layar, dan menutup aplikasi. Installer juga belum
ditandatangani (code signing). Karena itu antivirus **bisa** salah mengira
program ini berbahaya (*false positive*). Hal ini **belum diuji** pada
antivirus mana pun. Cara termudah mencegahnya: admin sekolah memberi
pengecualian **sebelum** memasang, di PC peserta dan PC pengawas.

**Microsoft Defender, per PC** (PowerShell sebagai Administrator):

```powershell
Add-MpPreference -ExclusionPath "C:\Program Files\LabSentinel"
Get-MpPreference | Select-Object -ExpandProperty ExclusionPath   # periksa
```

**Microsoft Defender, seluruh lab lewat Group Policy:** Computer Configuration
→ Administrative Templates → Windows Components → Microsoft Defender Antivirus
→ Exclusions → **Path Exclusions** → isi `C:\Program Files\LabSentinel` dengan
nilai `0`.

**Antivirus lain** (ESET, Kaspersky, Avast, Smadav, dll.): tambahkan folder
yang sama sebagai pengecualian di konsol manajemennya.

- Kecualikan **folder**, jangan nama proses. Folder di `Program Files` hanya
  bisa diubah administrator, jadi peserta tidak bisa menaruh program di sana.
  Pengecualian berdasarkan nama proses bisa diakali dengan mengganti nama
  game.
- Bila installer sudah terlanjur dikarantina, pulihkan dari karantina
  antivirus setelah pengecualian dipasang, lalu ulangi instalasi.

**SmartScreen** ("Windows protected your PC") tidak terpengaruh pengecualian
di atas. Peringatan ini muncul karena file installer bertanda "diunduh dari
internet". Hapus tanda itu sekali sebelum menyebarkan installer:

```powershell
Unblock-File .\LabSentinelAgent-Setup-*.exe
```

(atau klik kanan file → Properties → centang **Unblock** → OK). Bila
peringatan tetap muncul, klik **More info → Run anyway**.

## 3. Pasang dashboard (PC pengawas)

1. Jalankan `LabSentinelDashboard-Setup-<versi>.exe` sebagai Administrator.
2. Di akhir instalasi, biarkan **Jalankan Lab Sentinel Dashboard** tercentang.
3. Catat **kode pemasangan** 6 digit (konsol, atau dashboard → **Aturan →
   Pemasangan PC baru**).
4. Pastikan dashboard berada di **mode uji coba** (spanduk kuning di Beranda) —
   ini bawaan pemasangan baru.

Konfigurasi opsional: salin `config/dashboard.example.toml` menjadi
`dashboard.toml` di samping `labsentinel-dashboard.exe` (mis. nama dashboard,
retensi data).

## 4. Uji di satu PC dulu

Sebelum memasang ke seluruh lab:

1. Pasang agent di **satu** PC (§5).
2. Login sebagai peserta: ikon Lab Sentinel muncul di area notifikasi.
3. Di dashboard, PC tersebut muncul **Aktif** di **Layar PC** dengan
   thumbnail layarnya (bukan "layar tidak terjangkau").
4. Dalam **mode uji coba**, buka aplikasi di luar daftar izin → tercatat di
   **Laporan**, aplikasi tidak ditutup.
5. Jalankan bagian M6 di [manual-test-checklist.md](manual-test-checklist.md)
   untuk PC ini.

## 5. Pasang agent di PC peserta

### Satu per satu (tanpa terminal)

Salin `LabSentinelAgent-Setup-<versi>.exe` ke PC (flashdisk atau folder
bersama), lalu:

1. **Klik dua kali** installer, setujui permintaan Administrator (UAC), lalu
   klik **Install**.
2. Berkas tersalin, lalu jendela **Lab Sentinel Agent — Pasang PC ini**
   terbuka.
3. Ketik **kode pemasangan** 6 digit dari dashboard (halaman Aturan), klik
   **Pasangkan**.
4. Muncul **"Berhasil dipasangkan"**: layanan pemantauan sudah berjalan dan PC
   tampil di dashboard dalam beberapa detik. Klik **Selesai**.

Bila ada masalah, jendela menjelaskannya dan Anda bisa langsung mencoba lagi:
**Kode salah** (cek kode di dashboard), **Pendaftaran PC baru sedang ditutup**
(buka di dashboard), **Terlalu banyak percobaan** (tunggu 15 menit),
**Dashboard tidak ditemukan** (pastikan dashboard berjalan; bila lab beda
jaringan, isi **Alamat dashboard** di bagian opsional, mis. `192.168.1.10`).

Jendela yang sama bisa dibuka kapan saja dari menu Start → **Lab Sentinel →
Pasangkan PC ini ke dashboard** (mis. setelah dashboard diganti). Pada PC yang
sudah terpasang, jendela menampilkan statusnya dan tombol **Pasangkan ulang**.

### Banyak PC sekaligus (senyap)

Jalankan sebagai Administrator di tiap PC (lewat skrip login GPO, PDQ Deploy,
atau berbagi folder + `psexec`):

```bat
LabSentinelAgent-Setup-<versi>.exe /VERYSILENT /SUPPRESSMSGBOXES /PAIRCODE=123456
```

Tambahkan `/DASHBOARD=ws://192.168.1.10:8765` bila broadcast tidak tersedia.
Log instalasi ada di `%TEMP%\Setup Log *.txt`; hasil pairing tercatat di sana
(`labsentinel-agent.exe --pair ... -> 0` berarti berhasil). PC yang gagal
dipasangkan bisa diselesaikan lewat jendela di menu Start.

### Disk image / cloning

Agent menyimpan identitas PC (`agent_id`) dan kuncinya (`token`). Bila disk
di-clone **setelah** pairing, semua hasil clone berbagi identitas dan saling
memutus di dashboard. Pilih salah satu:

- **Disarankan:** di PC master, pasang agent **tanpa** memasangkan: tutup
  jendela pemasangan dengan **Batal**, atau pasang senyap tanpa `/PAIRCODE`.
  Layanan belum dipasang dan belum ada identitas apa pun di image. Clone; di
  tiap PC hasil clone buka menu Start → **Pasangkan PC ini ke dashboard** dan
  ketik kodenya (identitas dibuat saat itu, berbeda tiap PC).
- Bila image sudah terlanjur dipasangkan: di tiap PC hasil clone, sebagai
  Administrator, hentikan layanan dan hapus identitasnya, lalu pasangkan lagi
  dari menu Start:

```bat
sc stop LabSentinelAgent
del "C:\ProgramData\LabSentinel\agent\agent_id" "C:\ProgramData\LabSentinel\agent\token"
```

Cadangan lewat baris perintah (sebagai Administrator):
`"C:\Program Files\LabSentinel\Agent\labsentinel-agent.exe" --pair-only`
(meminta kode) lalu `sc start LabSentinelAgent`.

## 6. Tutup pendaftaran dan mulai menegakkan

1. Setelah semua PC tampil di **Layar PC**, buka **Aturan → Pemasangan PC
   baru** → **Tutup pendaftaran PC baru**.
2. Jalankan satu sesi praktikum dalam **mode uji coba**; periksa Laporan untuk
   aplikasi yang perlu ditambahkan ke daftar izin.
3. Matikan mode uji coba (**Aturan → Penindakan**).

## 7. Perawatan

| Tugas | Cara |
|---|---|
| **Upgrade** agent | Jalankan installer versi baru (senyap pun bisa); pairing dipertahankan, layanan dihentikan dan dijalankan lagi tanpa dilaporkan sebagai pelanggaran |
| **Upgrade** dashboard | Tutup dashboard, jalankan installer baru; database dipertahankan |
| PC diganti / dipasang ulang Windows | Pasang agent seperti PC baru (buka pendaftaran sementara) |
| Database dashboard hilang / diganti | Semua PC akan ditolak "perlu dipasangkan ulang"; buka pendaftaran, lalu di tiap PC buka menu Start → **Pasangkan PC ini ke dashboard** → **Pasangkan ulang** |
| Mencabut satu PC | Hapus agent dari PC itu (*Apps & features* → Lab Sentinel Agent) |
| **Hapus** agent | *Apps & features* → Lab Sentinel Agent. Layanan, aturan firewall, entri autostart, dan token PC dihapus; kebijakan browser dipulihkan |
| **Hapus** dashboard | *Apps & features* → Lab Sentinel Dashboard. Database, bukti, dan laporan di ProgramData **sengaja disimpan** |

## 8. Membangun installer dari kode sumber

Di PC Windows pengembang:

```powershell
python -m pip install -e ".[dashboard,agent,dev,packaging]"
python -m pytest                       # semua harus lulus
python packaging/build.py --installer  # butuh Inno Setup 6 (jrsoftware.org)
```

Hasil: `build/dist/LabSentinelAgent/`, `build/dist/LabSentinelDashboard/`, dan
installer di `build/installer/`. Installer belum ditandatangani; SmartScreen
akan memperingatkan saat dijalankan pertama kali.
