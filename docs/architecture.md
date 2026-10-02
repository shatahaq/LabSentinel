# Arsitektur Lab Sentinel

Dokumen ini menjelaskan desain sistem dan keputusan utama. Catatan keputusan
singkat ada di [adr/](adr/).

## 1. Gambaran umum

Dua komponen di LAN:

```
  PC Peserta (tiap lab PC)                         PC Pengawas
 ┌───────────────────────────┐                   ┌────────────────────────────┐
 │ Agent (Windows Service)    │   WebSocket/LAN   │ Dashboard                  │
 │  • enforcement whitelist   │  layar,           │  • WS server (asyncio)     │
 │  • kebijakan situs browser │  pelanggaran,     │  • SQLite (aturan, log,    │
 │  • baca judul jendela      │  heartbeat ───────▶   bukti)                   │
 │  • screenshot bukti        │                   │  • UI admin (Eel, localhost)│
 │  • tray indikator          │◀── aturan,        │  • beacon UDP discovery    │
 │  • watchdog + heartbeat    │    perintah       │                            │
 └───────────────────────────┘                   └────────────────────────────┘
```

Agent = **client**. Dashboard = **server WebSocket** untuk semua agent, plus
server Eel terpisah khusus UI admin di `localhost`. Pemisahan ini disengaja
([ADR-0001](adr/0001-transport-split.md)): Eel berbasis gevent dan tidak
dirancang sebagai server many-client; `websockets` (asyncio) menangani 40+ agent
di loop-nya sendiri, di thread terpisah dari UI.

## 2. Lapisan (layering)

Kode dipisah per tanggung jawab agar dapat diuji dan di-mock:

| Lapisan | Modul | Catatan |
|---------|-------|---------|
| **Transport** | `dashboard/.../transport/server.py`, `agent/.../transport/client.py` | Hanya soal socket, handshake, framing. Bebas dari logika enforcement/penyimpanan lewat callback & Protocol. |
| **Protokol/domain** | `shared/labsentinel_shared/protocol.py`, `rules.py`, `enums.py` | Skema pesan versi-ed (pydantic), model aturan. Murni, lintas-OS. |
| **Keamanan** | `shared/.../security.py` | Token (hash + constant-time compare), anti-replay. |
| **Discovery** | `shared/.../discovery.py` | Beacon UDP + probe. |
| **Enforcement** | `agent/.../enforce/*` (M2+) | Di balik interface; akses WMI/registry/proses di-mock saat test. |
| **Penyimpanan** | `dashboard/.../storage/*` (M3+) | SQLite, parameterized. |
| **UI** | `dashboard/web/*` + Eel bridge (M5+) | HTML/CSS/JS, design system tunggal. |

Dependency injection sederhana: transport menerima `ServerEvents` /
callback (`on_ruleset`, `on_command`, `metrics_provider`, dst.) sehingga domain
dan penyimpanan dapat diganti dengan *fake* dalam test.

## 3. Protokol pesan

Setiap pesan adalah satu `Envelope` JSON:

```jsonc
{
  "v": 1,                      // versi protokol; peer menolak versi tak dikenal
  "id": "<uuid hex>",          // unik — korelasi ACK & deteksi replay
  "ts": 1696200000.0,          // epoch detik — jendela anti-replay
  "token": "<pairing token>",  // diverifikasi transport, TIDAK PERNAH di-log
  "payload": { "type": "...", ... }
}
```

`payload` adalah *discriminated union* pydantic atas field `type`. Pesan yang
rusak, kelebihan field, oversize, atau salah bentuk **ditolak di batas** (drop),
tidak pernah membuat crash. Skema diekspor ke
`shared/schema/protocol.v1.schema.json` (lihat `scripts/export_schema.py`).

Tipe pesan:

- **Agent → Dashboard:** `hello`, `heartbeat`, `violation`, `screen_frame`,
  `ack`, `goodbye`.
- **Dashboard → Agent:** `welcome`, `reject`, `rules_update`, `command`.

### Handshake & pairing

1. Agent connect → kirim `hello` (berisi token, agent_id, hostname, revisi
   aturan yang di-cache).
2. Dashboard verifikasi versi protokol, token (constant-time), dan anti-replay.
   - Gagal → `reject` + tutup.
   - Sukses → `welcome` (session_id, interval heartbeat, `ruleset` lengkap).
3. Agent menerapkan ruleset, mulai heartbeat + receive loop.

## 4. Keamanan

- **Autentikasi token** antara agent & dashboard. Dashboard hanya menyimpan
  **hash SHA-256** token; agent menyimpan token mentah di file terpisah
  (`token_file`) — tidak ada secret hardcoded.
- **Anti-replay**: `ReplayGuard` menolak pesan di luar jendela waktu atau yang
  `id`-nya sudah pernah dilihat (memori dibatasi LRU).
- **Validasi skema** penuh pada setiap pesan masuk.
- **Least privilege**: hanya enforcement yang butuh SYSTEM (M4); logika lain
  berjalan dengan hak minimum.
- **TLS** (`wss://`) opsional — beacon membawa flag `tls`; sertifikat disiapkan
  per-lab (M7).
- Token & blob screenshot **di-redaksi** sebelum masuk log (`Envelope.redacted`).

## 5. Keandalan

- **Auto-reconnect** dengan exponential backoff + jitter (`BackoffPolicy`).
- **Rule cache** persisten & atomik (`RuleCache`): agent tetap menegakkan
  aturan terakhir saat terputus, dan memulihkannya saat restart; file korup
  diabaikan tanpa crash.
- Server mengisolasi error per-koneksi: satu koneksi buruk tidak menjatuhkan
  server.
- Logging terstruktur (key=value) dengan rotasi.

## 6. Privasi & data yang dikirim

Data yang dikirim agent: identitas PC (id, hostname, username login), state &
metrik ringan (CPU%, RAM, judul jendela aktif), pelanggaran, dan frame layar
**hanya** saat pemantauan aktif dan ada yang melihat. Tidak ada ketikan, audio,
atau video kamera. Retensi otomatis (`retention_days`) menghapus data lama.

## 7. Model threading

- Dashboard: UI Eel/gevent di thread utama; WS server asyncio di thread
  terpisah dengan loop-nya sendiri. Jembatan antar-keduanya memakai antrian
  thread-safe (M5).
- Agent: loop asyncio untuk transport; pekerjaan berat (hash, screenshot) di
  executor/thread agar tidak memblokir heartbeat (M2+).

## 8. Yang belum dibangun (per M1)

Enforcement, tray, screenshot, kebijakan registry, UI, penyimpanan, service,
dan watchdog menyusul di M2–M7 sesuai urutan di [README](../README.md). Hal-hal
yang butuh Windows nyata + admin diverifikasi lewat
[checklist uji manual](manual-test-checklist.md).
