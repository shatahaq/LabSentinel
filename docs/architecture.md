# Arsitektur Lab Sentinel

Dokumen ini menjelaskan desain sistem setelah M1–M7. Keputusan penting dicatat
singkat di [adr/](adr/); batasan yang diketahui ada di
[limitations.md](limitations.md).

## 1. Gambaran umum

```
 PC peserta (tiap PC lab)                                  PC pengawas
┌──────────────────────────────────────────────┐        ┌──────────────────────────────┐
│ session 0                                      │        │ labsentinel-dashboard.exe    │
│ ┌──────────────────────────────────────────┐ │  LAN   │ ┌──────────────────────────┐ │
│ │ labsentinel-service.exe  (SYSTEM)        │ │ WS     │ │ thread asyncio:          │ │
│ │  • aturan, keputusan, pencatatan         │◀┼────────┼▶│  server WebSocket :8765  │ │
│ │  • kebijakan browser (HKLM)              │ │ :8765  │ │  beacon UDP :50123       │ │
│ │  • koneksi dashboard, heartbeat          │ │        │ │  watchdog heartbeat      │ │
│ │  • run marker, watchdog loop             │ │◀ UDP ─┼─│  penjadwal sesi          │ │
│ └─────────▲──────────────────▲─────────────┘ │ 50123  │ ├──────────────────────────┤ │
│   loopback│token SYSTEM      │loopback token │        │ │ SQLite (WAL) + berkas    │ │
│ ──────────┼──────────────────┼───────────────│        │ │ bukti + laporan          │ │
│ sesi pengguna (konsol)       │               │        │ ├──────────────────────────┤ │
│ ┌─────────┴──────────────┐ ┌─┴─────────────┐ │        │ │ thread utama: Eel/gevent │ │
│ │ desktop worker (SYSTEM)│ │ helper tray   │ │        │ │ UI admin di localhost    │ │
│ │ jendela, judul, layar, │ │ (akun peserta)│ │        │ │ :8080 (Chrome/Edge)      │ │
│ │ WM_CLOSE, kunci layar  │ │ ikon + popup  │ │        │ └──────────────────────────┘ │
│ └────────────────────────┘ └───────────────┘ │        └──────────────────────────────┘
└──────────────────────────────────────────────┘
```

**Agent = client, dashboard = server.** Dashboard menjalankan server WebSocket
khusus untuk 40+ agent di loop asyncio-nya sendiri, terpisah dari Eel yang hanya
melayani UI admin di `localhost` ([ADR-0001](adr/0001-transport-split.md)).

Satu instalasi agent terdiri dari **tiga proses**:

| Proses | Berjalan sebagai | Tugas | ADR |
|---|---|---|---|
| `labsentinel-service.exe` | LocalSystem, session 0 | Semua keputusan: whitelist, kebijakan situs, sesi, pencatatan, koneksi dashboard | [0004](adr/0004-agent-two-process-split.md) |
| desktop worker (`labsentinel-agent.exe --desktop-worker`) | **SYSTEM di sesi pengguna** | Hanya mengamati/melaksanakan di desktop: daftar jendela, judul jendela aktif, tangkapan layar, menutup jendela, kunci layar | [0006](adr/0006-desktop-access-from-the-service.md) |
| `labsentinel-tray.exe` | akun peserta | Ikon tray dan popup peringatan (transparansi) | [0004](adr/0004-agent-two-process-split.md) |

Layanan tidak bisa melihat desktop dari session 0, karena itu ada worker.
Peserta (pengguna standar) tidak dapat mengakhiri layanan maupun worker; helper
tray boleh dimatikan, tetapi akan dinyalakan lagi oleh layanan dan selama hilang
dashboard menampilkan "indikator disembunyikan".

## 2. Peta modul

| Lapisan | Modul | Catatan |
|---|---|---|
| Protokol | `shared/.../protocol.py`, `enums.py`, `rules.py` | Skema pesan pydantic versi-ed; diekspor ke `shared/schema/protocol.v1.schema.json` |
| Keamanan | `shared/.../security.py`, `dashboard/.../domain/pairing.py` | Kode pairing, token per-agent (hash), rate limiter, anti-replay |
| Discovery | `shared/.../discovery.py` | Beacon & probe UDP |
| Transport | `agent/.../transport/client.py`, `dashboard/.../transport/server.py` | Handshake, framing, reconnect; tanpa logika domain |
| Enforcement | `agent/.../enforce/*` | Safety gate (deny-by-default), matcher, engine, deteksi judul |
| Kebijakan browser | `agent/.../policy/*` | Backup→tulis→pulihkan kebijakan Chrome/Edge di HKLM ([ADR-0003](adr/0003-site-blocking-via-policy.md)) |
| Desktop | `agent/.../desktop/*` | Proxy (layanan), worker (sesi pengguna), supervisor/spawn, pemilihan jalur |
| Siklus hidup | `agent/.../lifecycle.py`, `service.py`, `autostart.py` | Satu instans, run marker, watchdog, Windows Service, Run key tray |
| Platform | `agent/.../platform/*` | psutil/pywin32/WMI/mss/ACL; di-*mock* saat test |
| Domain dashboard | `dashboard/.../domain/*` | Layanan PC, perintah ber-ACK, sesi & jadwal, aturan, event bus |
| Penyimpanan | `dashboard/.../storage/db.py` | SQLite parameterized, WAL, satu koneksi berkunci |
| Laporan | `dashboard/.../reports/*` | Satu model → PDF (ReportLab) dan Excel (openpyxl) |
| UI | `dashboard/web/*`, `ui/bridge.py` | HTML/CSS/JS tanpa framework ([ADR-0005](adr/0005-ui-design-system.md)); jembatan Eel↔asyncio |

## 3. Protokol

Setiap pesan adalah satu `Envelope` JSON `{v, id, ts, token, payload}`.
`payload` adalah *discriminated union* atas `type`. Pesan rusak, kelebihan
field, terlalu besar, atau versi tak dikenal **ditolak di batas**, tidak pernah
membuat crash.

- Agent → dashboard: `hello`, `heartbeat`, `violation`, `screen_frame`, `ack`, `goodbye`.
- Dashboard → agent: `welcome`, `reject`, `rules_update`, `command`.

`heartbeat` membawa status nyata agent (bukan tombol terakhir yang ditekan),
`indicator_visible` (ikon peserta tampil?) dan `desktop_access` (layanan bisa
melihat desktop?).

### Pemasangan (pairing)

1. Admin membaca **kode 6 digit** di dashboard (halaman Aturan).
2. Admin mengetik kode di jendela pemasangan (`labsentinel-setup.exe`), atau
   installer senyap menjalankan `labsentinel-agent --pair KODE --pair-only`;
   `hello` membawa kode, tanpa token.
3. Dashboard memeriksa: pendaftaran terbuka? kode cocok (constant-time)? alamat
   sumber tidak terkunci (5 salah → kunci 15 menit)? Lalu menerbitkan **token
   acak per-PC**, menyimpan **hash**-nya, mengirimnya sekali di `welcome`.
4. Agent menyimpan token (atomik; ACL SYSTEM+Administrators). Sejak itu hanya
   token itu yang dipakai; kode tidak dibutuhkan lagi.

Token yang tidak dikenal (dicabut / database diganti) → `reject` "perlu
dipasangkan ulang"; agent terus mencoba dengan backoff. Kode yang ditolak →
agent berhenti dan menjelaskan (mencoba ulang hanya akan mengunci PC sendiri).
Semua percobaan tercatat di catatan aktivitas.

## 4. Keamanan

- Token per-PC, disimpan sebagai hash SHA-256 di dashboard; token PC satu tidak
  berlaku untuk PC lain. Kode pairing bukan kredensial tetap.
- Anti-replay (`ReplayGuard`), validasi skema penuh, batas ukuran pesan.
- Token, kode pairing, dan blob gambar **diredaksi** sebelum masuk log.
- Agent terhubung **langsung** ke dashboard (`proxy=None`), tidak lewat proxy
  sistem sekolah.
- Kanal loopback: helper tray memakai token yang bisa dibaca pengguna dan
  **tidak punya wewenang** apa pun; desktop worker memakai token yang hanya bisa
  dibaca SYSTEM/Administrators.
- **Tidak ada TLS**: lalu lintas LAN berupa teks biasa. Lihat
  [limitations.md](limitations.md) untuk dampak dan mitigasinya.

## 5. Anti-tamper dan keandalan

| Mekanisme | Di mana | Efek |
|---|---|---|
| Windows Service, auto-start, LocalSystem | `service.py` | Berjalan sebelum login; peserta tidak bisa menghentikannya |
| Recovery actions (5 s, 5 s, 30 s; juga pada exit ≠ 0) | `service.py` | Proses yang dibunuh/crash hidup lagi |
| Run marker | `lifecycle.RunMarker` | Start berikutnya melaporkan: dihentikan paksa / listrik padam / layanan dihentikan manual / dimulai ulang watchdog |
| Watchdog loop | `lifecycle.LoopWatchdog` | Agent macet keluar sendiri (kode 70) agar di-restart |
| Satu instans | `lifecycle.InstanceLock` | Salinan kedua menolak berjalan |
| Supervisor worker & tray | `desktop/spawn.py` | Worker selalu ada di sesi konsol; indikator dinyalakan lagi bila hilang |
| Run key tray + pemulihan | `autostart.py` | Indikator muncul di setiap login; entri yang dihapus dipulihkan |
| Kebijakan browser selalu dipulihkan | `app.shutdown`, `policy/*` | Berhenti normal, mati paksa, atau crash: kebijakan institusi kembali utuh |
| Rule cache atomik | `rule_cache.py` | Aturan terakhir tetap ditegakkan saat dashboard tak terjangkau |
| Reconnect dengan backoff + jitter | `transport/client.py` | 40 PC tidak menyerbu dashboard bersamaan |

## 6. Privasi

Yang dikirim agent: identitas PC, status & metrik ringan, judul jendela aktif,
pelanggaran (+ satu tangkapan layar bukti), dan frame layar **hanya** saat
pemantauan aktif dan ada yang melihat di dashboard. **Tidak ada** ketikan,
suara, atau kamera — tidak ada kodenya sama sekali. Peserta selalu melihat
indikator. Data lama dihapus otomatis (`retention_days`, default 30 hari).

## 7. Model threading

- **Dashboard**: Eel/gevent di thread utama; asyncio (server agent, beacon,
  watchdog, sesi, retensi) di thread `ls-agents`. Dijembatani `UiBridge`:
  UI→domain lewat `run_coroutine_threadsafe`, domain→UI lewat buffer event yang
  di-*poll*. Laporan dibuat di thread UI.
- **Layanan agent**: satu loop asyncio (transport, deteksi, streaming, judul,
  supervisor); pekerjaan berat di executor. `DesktopProxy` punya thread+loop
  sendiri agar panggilan sinkron dari loop utama tidak deadlock. Watchdog di
  thread daemon.
- **Desktop worker**: satu loop asyncio; setiap operasi desktop di thread.

## 8. Data

Dashboard (`C:\ProgramData\LabSentinel\dashboard\`): `labsentinel.sqlite`
(PC, token-hash, pelanggaran, perintah, sesi, aturan, catatan aktivitas,
pengaturan), `screenshots\<tanggal>\*.jpg`, `reports\*.pdf|xlsx`, `logs\`.

Agent (`C:\ProgramData\LabSentinel\agent\`): `agent_id`, `token` (ACL
SYSTEM+Admin), `ruleset.json` (cache), `policy_backup.json` (hanya saat
kebijakan terpasang), `running.json` (run marker), `agent.lock`,
`desktop_endpoint`/`desktop_token`, `tray_endpoint`/`tray_token`, `logs\`.
Log helper tray ada di `%LOCALAPPDATA%\LabSentinel\` tiap pengguna.

## 9. Packaging dan deploy

`packaging/build.py` membangun dua folder PyInstaller (agent: empat exe berbagi
satu runtime — `labsentinel-setup.exe` jendela pemasangan, `labsentinel-service.exe`,
`labsentinel-agent.exe`, `labsentinel-tray.exe`; dashboard: exe + aset UI) dan,
di Windows, dua installer Inno Setup. Installer agent yang diklik dua kali
langsung membuka jendela pemasangan; `/PAIRCODE=` memasangkan tanpa jendela.
`.github/workflows/windows.yml` membangun keduanya di runner Windows dan menguji
pemasangan dari installer itu sungguhan. Lihat [deploy-guide.md](deploy-guide.md) dan
[admin-guide.md](admin-guide.md).

## 10. Strategi pengujian

- Unit & integrasi (pytest, ~410 test) berjalan di Linux maupun Windows:
  transport lewat socket sungguhan, pairing lewat WebSocket sungguhan, agent
  utuh melawan dashboard sungguhan, proses agent nyata dibunuh `SIGKILL`,
  laporan PDF/Excel dibaca balik, worker desktop lewat loopback.
- Akses Windows (WMI, registry HKLM, Chrome/Edge asli, layanan, sesi pengguna)
  di-*mock* di test dan diverifikasi lewat
  [manual-test-checklist.md](manual-test-checklist.md).
- `ruff` dan `mypy --strict` (diperiksa dengan `--platform win32`).
