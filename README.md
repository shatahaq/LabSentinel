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
| **Agent** | tiap PC peserta | Windows Service latar belakang: menegakkan whitelist aplikasi, mengelola kebijakan situs browser, mengirim layar & pelanggaran, menampilkan indikator tray. |
| **Dashboard** | PC pengawas | Server WebSocket untuk agent + database SQLite + UI admin (Eel). |

Agent adalah **client**; dashboard menjalankan **server WebSocket khusus** untuk
40+ agent, sementara Eel hanya melayani UI admin di `localhost`
([ADR-0001](docs/adr/0001-transport-split.md)). Agent menemukan dashboard
otomatis lewat **UDP broadcast** — tidak perlu mengatur IP satu per satu.

## Struktur repо

```
shared/     labsentinel_shared/   protokol pesan versi-ed, rules, security, discovery, config, logging
agent/      labsentinel_agent/    client transport, rule cache (+ enforcement, tray — milestone berikut)
dashboard/  labsentinel_dashboard/ server transport (+ storage, UI — milestone berikut)
docs/       arsitektur, ADR, checklist uji manual
scripts/    export_schema.py, m1_smoke.py
tests/      unit/ + integration/
```

## Menjalankan test

```powershell
# dari root repo
$env:PYTHONPATH = "shared;agent;dashboard"
python -m pytest            # unit + integration
python -m ruff check .      # lint
```

Demo M1 langsung (server + agent di satu proses, loopback):

```powershell
python scripts/m1_smoke.py
```

## Status milestone

| Milestone | Isi | Status |
|-----------|-----|--------|
| **M1** | Protokol + transport dasar: UDP discovery, WebSocket, heartbeat, pairing, auth token, anti-replay, auto-reconnect, rule cache | ✅ Selesai & teruji (40 test lulus) |
| M2 | Penegakan whitelist aman + peringatan + screenshot bukti + dry-run | ⏳ |
| M3 | Kontrol jeda/lanjut dari dashboard + audit log + ikon tray | ⏳ |
| M4 | Pemblokiran situs via kebijakan browser (registry) + pencatatan judul jendela | ⏳ |
| M5 | Streaming layar adaptif + grid dashboard + notifikasi real-time | ⏳ |
| M6 | Mode/profil, laporan PDF/Excel, anti-tamper (service, watchdog) | ⏳ |
| M7 | Polish UI/UX, optimasi performa, packaging + dokumentasi | ⏳ |

Lihat [docs/architecture.md](docs/architecture.md) untuk desain lengkap dan
[docs/manual-test-checklist.md](docs/manual-test-checklist.md) untuk uji yang
butuh Windows nyata + hak admin.

## Lingkungan pengembangan

- Python **3.11+** (dikembangkan & diuji pada 3.14).
- Dependensi inti lintas-OS; paket khusus Windows (`pywin32`, `wmi`, `mss`,
  `pystray`) dipasang lewat extra `agent` dengan marker platform.

```powershell
python -m pip install -e ".[dashboard,agent,dev]"   # pada PC Windows nyata
```

## Batasan yang diketahui

- Hanya Windows; hanya browser Chromium (Chrome/Edge) untuk pemblokiran situs.
- Pencatatan situs dari **judul jendela** kurang presisi dibanding extension —
  whitelist aplikasi adalah pagar utama. Lihat [ADR-0003](docs/adr/0003-site-blocking-via-policy.md).
- Hanya untuk satu LAN; kualitas streaming bergantung kapasitas jaringan.
