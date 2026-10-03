# ADR-0004: Agent dipecah menjadi service + helper sesi pengguna

**Status:** Diterima · **Tanggal:** 2026-10-03

## Konteks

Agent harus memenuhi dua kebutuhan yang bertabrakan secara teknis:

1. **Anti-tamper**: berjalan sebagai Windows Service agar peserta non-admin tidak
   bisa mematikannya, dan agar bisa menulis kebijakan browser di `HKLM`.
2. **Transparansi**: peserta **selalu** melihat indikator bahwa pemantauan aktif
   (ikon tray) dan menerima popup peringatan sebelum aplikasi ditutup.

Windows Service berjalan di **session 0**, yang terisolasi dari desktop pengguna
sejak Windows Vista. Proses di session 0 tidak dapat menggambar jendela, ikon
tray, atau notifikasi yang terlihat oleh pengguna yang login.

## Keputusan

Satu instalasi agent terdiri dari **dua proses**:

| Proses | Berjalan sebagai | Tugas |
|--------|------------------|-------|
| **Service** | SYSTEM, session 0 | Penegakan whitelist, kebijakan registry, koneksi ke dashboard, screenshot |
| **Helper tray** | pengguna yang login, sesi desktop | Ikon tray + popup peringatan |

Keduanya berkomunikasi lewat **TCP loopback** (`127.0.0.1`) dengan pesan JSON
baris-demi-baris, diautentikasi dengan token yang ditulis service ke file.

## Pembagian wewenang (penting)

Kanal ini **hanya untuk menampilkan**, bukan untuk mengendalikan:

- **service → helper**: status, peringatan, pesan broadcast.
- **helper → service**: hanya `hello` dan `alive` ("ikon masih tampil").

Service **mengabaikan** pesan lain dari helper. Jadi peserta yang mengganti atau
memalsukan helper **tidak bisa** menjeda pemantauan atau mengubah penegakan
aturan. Hal terburuk yang bisa dilakukan peserta adalah menghentikan tampilnya
indikator — dan itu terdeteksi: service melaporkan `indicator_visible=false`
lewat heartbeat sehingga dashboard dapat menandai PC tersebut.

## Alasan memilih TCP loopback

- Bekerja lintas batas session 0 tanpa ACL named-pipe yang rumit.
- Dapat diuji otomatis di CI/lingkungan pengembangan (socket biasa), berbeda
  dengan named pipe Windows yang sulit diuji.
- Terikat ke `127.0.0.1` saja, jadi tidak pernah terjangkau dari jaringan.

## Konsekuensi

- Helper harus dijalankan otomatis saat pengguna login (lewat `Run` key atau
  scheduled task; dipasang oleh installer di M7).
- Helper bisa mati tanpa mematikan penegakan aturan — penegakan tetap jalan di
  service. Yang hilang hanya indikator, dan itu dilaporkan ke dashboard.
- Watchdog (M6) menyalakan ulang helper bila hilang, demi menjaga transparansi.

## Alternatif yang ditolak

- *Agent sebagai aplikasi biasa di sesi pengguna (tanpa service)*: peserta bisa
  mematikannya lewat Task Manager, dan tidak bisa menulis kebijakan `HKLM`.
- *`WTSSendMessage` dari session 0*: hanya kotak dialog sederhana, tidak bisa
  ikon tray, dan dialognya merebut fokus.
- *Named pipe*: layak di produksi, tapi lebih sulit diuji otomatis; dapat
  dipertimbangkan kemudian tanpa mengubah protokol pesannya.
