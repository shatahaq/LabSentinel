# Lab Sentinel

**Sistem pemantauan dan kontrol aktivitas lab komputer melalui LAN.**
Transparan, berbasis *whitelist*, dan hanya untuk perangkat milik institusi.

> ⚖️ **Etika — tidak dapat dinegosiasikan.** Lab Sentinel hanya digunakan secara
> transparan pada perangkat milik institusi. Peserta selalu melihat indikator
> pemantauan aktif. **Tidak ada** mode tersembunyi, keylogger, atau perekaman
> keyboard/mikrofon/webcam. Batasan ini ditegakkan di dalam kode, bukan sekadar
> kebijakan.

---

## Komponen

| Komponen | Lokasi | Peran |
|----------|--------|-------|
| **Agent** | tiap PC peserta | Tiga proses dalam satu instalasi: **layanan Windows** (aturan, kebijakan situs, koneksi dashboard, anti-tamper), **desktop worker** (dijalankan layanan sebagai SYSTEM di sesi pengguna: layar, jendela, judul) dan **helper tray** (ikon & popup untuk peserta). Lihat [ADR-0004](docs/adr/0004-agent-two-process-split.md) dan [ADR-0006](docs/adr/0006-desktop-access-from-the-service.md). |
| **Dashboard** | PC pengawas | Server WebSocket untuk agent + SQLite + UI admin (Eel) + laporan PDF/Excel. |

Agent adalah **client**; dashboard menjalankan **server WebSocket khusus** untuk
40+ agent, sementara Eel hanya melayani UI admin di `localhost`
([ADR-0001](docs/adr/0001-transport-split.md)). Agent menemukan dashboard
otomatis lewat **UDP broadcast** dan dipasangkan sekali dengan **kode 6 digit**
yang tampil di dashboard.

## Dokumentasi

| Untuk | Dokumen |
|---|---|
| Pengawas / laboran (pemakaian sehari-hari) | [docs/admin-guide.md](docs/admin-guide.md) |
| Memasang di lab | [docs/deploy-guide.md](docs/deploy-guide.md) |
| Desain sistem | [docs/architecture.md](docs/architecture.md), [docs/adr/](docs/adr/) |
| Yang tidak dilakukan / belum terverifikasi | [docs/limitations.md](docs/limitations.md) |
| Uji yang butuh Windows nyata + admin | [docs/manual-test-checklist.md](docs/manual-test-checklist.md) |
| Performa agent | [docs/performance.md](docs/performance.md) |

## Struktur repo

```
shared/     labsentinel_shared/    protokol versi-ed, rules, security, discovery, config, logging
agent/      labsentinel_agent/     layanan, transport, enforcement, kebijakan browser, desktop worker,
                                   tray, lifecycle (anti-tamper)
dashboard/  labsentinel_dashboard/ server transport, domain (pairing, sesi, aturan), storage, laporan
            web/                   UI admin (HTML/CSS/JS, tanpa framework)
packaging/  spesifikasi PyInstaller, skrip installer Inno Setup, build.py
docs/       arsitektur, ADR, panduan admin & deploy, batasan, checklist uji manual
scripts/    demo_lab.py, export_schema.py, m1_smoke.py, tray_demo.py, measure_agent.py
tests/      unit/ + integration/
```

## Menjalankan test

```powershell
# dari root repo
$env:PYTHONPATH = "shared;agent;dashboard"
python -m pytest            # unit + integration
python -m ruff check .      # lint
```

## Mencoba dashboard

Menjalankan dashboard sungguhan dengan PC simulasi (yang benar-benar melakukan
pairing) — cara tercepat melihat tampilan dan alur kerjanya tanpa lab:

```powershell
python scripts/demo_lab.py --pcs 14
```

Menjalankan dashboard dan satu agent dari kode sumber:

```powershell
labsentinel-dashboard --keep-running          # mencetak kode pemasangan 6 digit
labsentinel-agent --pair 123456               # sekali; berikutnya cukup: labsentinel-agent
```

Membangun installer (Windows, butuh Inno Setup 6):

```powershell
python packaging/build.py --installer
```

## Status milestone

| Milestone | Isi | Status |
|-----------|-----|--------|
| **M1** | Protokol + transport dasar: UDP discovery, WebSocket, heartbeat, pairing, auth token, anti-replay, auto-reconnect, rule cache | ✅ |
| **M2** | Penegakan whitelist aman + peringatan + screenshot bukti + dry-run | ✅ |
| **M3** | Kontrol jeda/lanjut (per-PC & massal, ber-ACK) + audit log + sesi + ikon tray peserta | ✅ |
| **M4** | Pemblokiran situs via kebijakan browser (registry) + pencatatan judul jendela | ✅ |
| **M5** | Streaming layar adaptif + UI dashboard (grid, aturan, laporan, audit) | ✅ |
| **M6** | Pairing 6 digit (+UI), laporan PDF/Excel per sesi, anti-tamper (Windows Service, watchdog, deteksi agent dihentikan, autostart tray), desktop worker | ✅ teruji otomatis · ⚠️ bagian Windows belum diverifikasi |
| **M7** | Packaging PyInstaller + installer, panduan admin & deploy, arsitektur final, daftar batasan | ✅ build Linux teruji · ⚠️ installer belum dikompilasi |

Status terakhir: **411 test lulus** (7 dilewati: butuh Windows/layar), `ruff` dan
`mypy --strict` bersih (`mypy --platform win32`).

**Penting:** putaran M6–M7 dikembangkan di Linux. Layanan Windows, desktop
worker di sesi pengguna, registry HKLM, Chrome/Edge asli, dan installer **belum
dijalankan di Windows**; daftar lengkap ada di
[docs/limitations.md](docs/limitations.md) §A dan
[docs/manual-test-checklist.md](docs/manual-test-checklist.md).

## Lingkungan pengembangan

- Python **3.11+** (dikembangkan & diuji pada 3.14).
- Dependensi inti lintas-OS; paket khusus Windows (`pywin32`, `wmi`, `mss`,
  `pystray`) dipasang lewat extra `agent` dengan marker platform.

```powershell
python -m pip install -e ".[dashboard,agent,dev,packaging]"   # pada PC Windows nyata
```

## Batasan yang diketahui (ringkas)

- Hanya Windows 10/11; pemblokiran situs hanya Chrome/Edge; hanya monitor utama
  dan sesi konsol.
- Lalu lintas agent↔dashboard **tidak dienkripsi** (tanpa TLS) — gunakan VLAN lab.
- Deteksi situs dari judul jendela kurang presisi; whitelist aplikasi adalah
  pagar utama ([ADR-0003](docs/adr/0003-site-blocking-via-policy.md)).
- Akun peserta wajib non-administrator.

Selengkapnya: [docs/limitations.md](docs/limitations.md).
