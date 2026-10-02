# ADR-0001: Dashboard = server WebSocket khusus, Eel hanya untuk UI

**Status:** Diterima · **Tanggal:** 2026-10-02

## Konteks

Dokumen produk menyebut dua hal: UI dashboard memakai **Eel** (gevent), dan
komunikasi agent↔dashboard memakai **WebSocket**. Perlu diputuskan siapa yang
menjadi server WebSocket untuk agent, dan bagaimana ia hidup berdampingan
dengan Eel.

## Keputusan

- **Dashboard menjalankan server WebSocket khusus** (library `websockets`,
  asyncio) di thread + event loop tersendiri untuk melayani 40+ agent.
- **Eel hanya melayani UI admin di `localhost`**.
- **Agent adalah client** WebSocket.

## Alasan

- Eel/gevent-websocket tidak dirancang sebagai server many-client berperforma;
  memaksakannya untuk 40+ agent berisiko pada latensi & stabilitas.
- Memisahkan transport agent dari UI memberi isolasi kegagalan dan membuat
  transport dapat diuji tanpa menjalankan UI.
- Agent sebagai client menyederhanakan jaringan: tidak perlu membuka port/
  firewall di tiap PC peserta, dan discovery berjalan satu arah (agent mencari
  dashboard).

## Konsekuensi

- Perlu jembatan thread-safe antara loop asyncio WS dan dunia gevent Eel
  (antrian) — dibangun di M5.
- Dua port di PC pengawas: WS (default 8765) untuk agent, Eel (default 8080)
  untuk UI lokal.

## Alternatif yang ditolak

- *Satu stack gevent untuk UI + agent*: lebih sedikit dependency, tapi berisiko
  untuk skala 40+ agent.
- *Agent sebagai server, dashboard polling tiap agent*: membalik discovery dan
  memaksa membuka port di tiap PC peserta — lebih rapuh.
