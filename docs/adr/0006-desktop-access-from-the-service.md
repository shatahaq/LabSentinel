# ADR-0006: Akses desktop dari layanan lewat *desktop worker*

**Status:** Diterima · **Tanggal:** 2026-10-05 · **Mengoreksi:** ADR-0004 (pembagian "screenshot di service")

## Konteks

Sejak M6 agent berjalan sebagai Windows Service (session 0) agar peserta tidak
bisa mematikannya. Saat menyiapkan dokumentasi M7 ditemukan bahwa banyak fungsi
agent **bergantung pada desktop pengguna**, dan dari session 0 desktop itu tidak
terlihat:

| Fungsi | Dari session 0 |
|---|---|
| Indeks jendela terlihat (`EnumWindows`) — syarat aturan 5 *safety gate* | kosong → **tidak ada aplikasi yang pernah dievaluasi**: whitelist mati diam-diam |
| Tangkapan layar (mss) untuk bukti dan streaming | hitam / gagal |
| Judul jendela aktif (deteksi situs, heartbeat) | kosong |
| `WM_CLOSE` (tutup aplikasi dengan sopan) | tidak sampai |
| `LockWorkStation` | gagal |

Uji M2–M5 di Windows dijalankan dari konsol pengguna, tempat semua ini bekerja,
sehingga celah ini baru tampak ketika agent menjadi layanan. ADR-0004 menulis
"screenshot di service" — itu tidak mungkin.

## Keputusan

Satu proses ketiga, **desktop worker** (`labsentinel-agent --desktop-worker`):

- **Dijalankan oleh layanan** di sesi konsol aktif, **sebagai SYSTEM**, pada
  desktop interaktif (`winsta0\default`), memakai salinan token layanan yang
  dipindah ke sesi tersebut (`DuplicateTokenEx` + `TokenSessionId` +
  `CreateProcessAsUser`). Peserta (pengguna standar) tidak dapat mengakhirinya.
- **Hanya mengamati dan melaksanakan**: daftar jendela, judul jendela aktif,
  tangkapan layar, menutup jendela milik suatu PID, mengunci layar. Semua aturan,
  keputusan, dan pencatatan tetap di layanan.
- **Tersambung balik ke layanan** lewat TCP loopback dengan token dari berkas
  yang hanya bisa dibaca SYSTEM/Administrators — peserta tidak bisa menyamar
  sebagai worker untuk mengirim tangkapan layar palsu.
- Di layanan, `DesktopProxy` melayani worker di **thread + event loop sendiri**,
  sehingga pertanyaan sinkron dari loop utama enforcement tidak pernah deadlock.
- `WorkerSupervisor` memeriksa tiap 3 detik: ada orang di konsol? worker untuk
  sesi itu tersambung? Bila tidak, jalankan satu (dibatasi sekali per 15 detik).
  Pergantian pengguna cepat: worker sesi baru menggantikan yang lama, yang lama
  diminta keluar.
- Saat worker (kembali) tersambung, agent **menyapu ulang** aplikasi yang sedang
  berjalan.
- Bila worker tidak ada padahal ada pengguna di konsol, heartbeat membawa
  `desktop_access=false` dan dashboard menampilkan **"layar tidak terjangkau"**
  pada kartu PC. Agent tidak menebak: tanpa daftar jendela, *safety gate*
  melindungi semua proses (gagal-aman), dan itu dikatakan terang-terangan.

Saat dijalankan dari konsol (pengembangan), tidak ada worker; API lokal dipakai
seperti sebelumnya.

## Alternatif yang dipertimbangkan

- *Helper tray (akun peserta) sekalian mengerjakan ini*: paling sederhana, tapi
  peserta bisa mematikannya dari Task Manager dan seketika whitelist berhenti
  bekerja. Ditolak.
- *Melonggarkan aturan 5 safety gate saat di layanan*: berisiko menutup proses
  latar pengguna (IME, OneDrive, sinkronisasi). Ditolak — keselamatan PC lebih
  penting daripada penegakan.
- *Menjalankan agent di sesi pengguna tanpa layanan*: kembali ke masalah
  ADR-0004 (bisa dimatikan, tidak bisa menulis HKLM).

## Konsekuensi

- Logika protokol, proxy, worker, supervisor, dan pemilihan jalur diuji otomatis
  (`tests/integration/test_desktop_worker.py`, termasuk agent mode-layanan utuh
  melawan dashboard sungguhan). **Pemanggilan Windows-nya sendiri
  (`CreateProcessAsUser` ke sesi lain, mss/EnumWindows sebagai SYSTEM di sesi
  pengguna) belum bisa diverifikasi tanpa Windows** — masuk checklist uji manual.
- Sesi RDP tidak dipantau (hanya sesi konsol). Lab memakai konsol.
- Proses SYSTEM di desktop pengguna tidak membuat jendela apa pun dan biner-nya
  di Program Files (tidak bisa ditulis pengguna).
