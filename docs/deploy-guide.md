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

### Satu per satu

1. Jalankan `LabSentinelAgent-Setup-<versi>.exe` sebagai Administrator.
2. Masukkan **kode pemasangan** 6 digit. Alamat dashboard boleh dikosongkan.
3. Selesai: layanan **Lab Sentinel Agent** berjalan dan PC muncul di dashboard
   dalam beberapa detik.

Bila kode ditolak atau dashboard tidak ditemukan, installer menjelaskan
penyebabnya. Agent tetap terpasang tetapi belum aktif; setelah masalahnya
beres, jalankan sebagai Administrator:

```bat
"C:\Program Files\LabSentinel\Agent\labsentinel-agent.exe" --pair-only
sc start LabSentinelAgent
```

### Banyak PC sekaligus (senyap)

Jalankan sebagai Administrator di tiap PC (lewat skrip login GPO, PDQ Deploy,
atau berbagi folder + `psexec`):

```bat
LabSentinelAgent-Setup-<versi>.exe /VERYSILENT /SUPPRESSMSGBOXES /PAIRCODE=123456
```

Tambahkan `/DASHBOARD=ws://192.168.1.10:8765` bila broadcast tidak tersedia.
Log instalasi ada di `%TEMP%\Setup Log *.txt`; hasil pairing tercatat di sana
(`labsentinel-agent.exe --pair ... -> 0` berarti berhasil).

### Disk image / cloning

Agent menyimpan identitas PC (`agent_id`) dan kuncinya (`token`). Bila disk
di-clone **setelah** pairing, semua hasil clone berbagi identitas dan saling
memutus di dashboard. Pilih salah satu:

- **Disarankan:** pasang agent di PC master **tanpa** kode (kosongkan kolom
  kode, atau senyap tanpa `/PAIRCODE`). Installer tidak memasangkan dan tidak
  menjalankan layanan, jadi image tidak membawa identitas apa pun. Clone; di
  tiap PC hasil clone, layanan membuat identitasnya sendiri saat boot lalu
  menunggu dipasangkan. Jalankan sebagai Administrator:
  `labsentinel-agent.exe --pair-only` (meminta kode) lalu
  `sc start LabSentinelAgent`.
- Bila image sudah terlanjur dipasangkan: di tiap PC hasil clone, sebagai
  Administrator:

```bat
sc stop LabSentinelAgent
del "C:\ProgramData\LabSentinel\agent\agent_id" "C:\ProgramData\LabSentinel\agent\token"
"C:\Program Files\LabSentinel\Agent\labsentinel-agent.exe" --pair-only
sc start LabSentinelAgent
```

## 6. Tutup pendaftaran dan mulai menegakkan

1. Setelah semua PC tampil di **Layar PC**, buka **Aturan → Pemasangan PC
   baru** → **Tutup pendaftaran PC baru**.
2. Jalankan satu sesi praktikum dalam **mode uji coba**; periksa Laporan untuk
   aplikasi yang perlu ditambahkan ke daftar izin.
3. Matikan mode uji coba (**Aturan → Perilaku**).

## 7. Perawatan

| Tugas | Cara |
|---|---|
| **Upgrade** agent | Jalankan installer versi baru (senyap pun bisa); pairing dipertahankan, layanan dihentikan dan dijalankan lagi tanpa dilaporkan sebagai pelanggaran |
| **Upgrade** dashboard | Tutup dashboard, jalankan installer baru; database dipertahankan |
| PC diganti / dipasang ulang Windows | Pasang agent seperti PC baru (buka pendaftaran sementara) |
| Database dashboard hilang / diganti | Semua PC akan ditolak "perlu dipasangkan ulang"; buka pendaftaran dan jalankan `labsentinel-agent.exe --pair-only` di tiap PC |
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
