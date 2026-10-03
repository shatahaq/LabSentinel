# ADR-0005: Design system UI dashboard

**Status:** Diterima · **Tanggal:** 2026-10-03

## Konteks

Dashboard dipakai dosen dan laboran yang **bukan orang teknis**, sering di
proyektor atau layar lab 1366×768, sambil mengawasi puluhan PC. Keputusan
visual di sini menentukan apakah pengawas bisa menangkap keadaan lab dalam
sekali lihat.

## Keputusan

Satu design system dengan token terpusat ([tokens.css](../../dashboard/web/css/tokens.css)):

1. **Padat informasi, bukan dekoratif.** Tipografi kecil (13,5px dasar), garis
   batas tipis 1px sebagai struktur utama, bayangan hanya untuk dialog dan
   toast. Radius 4–8px, bukan sudut sangat bulat.
2. **Satu warna aksen.** Netral abu-abu + satu biru. Warna semantik
   (hijau/kuning/merah) **hanya** untuk status mesin, sehingga titik merah
   selalu berarti hal yang sama.
3. **Status tidak pernah hanya warna.** Setiap status membawa ikon berbeda
   *dan* kata: "Aktif" ●, "Dijeda" ‖, "Terputus" ⚠, "Offline" ○. Memenuhi
   syarat aksesibilitas dan tetap terbaca di proyektor berwarna pucat.
4. **Ikon SVG inline, bukan emoji.** Emoji berbeda bentuk antar sistem dan
   tidak mewarisi warna teks.
5. **Angka `tabular-nums`.** Kolom angka tidak bergeser saat nilai berubah.
6. **Mode gelap dirancang terpisah**, bukan hasil pembalikan warna: permukaan
   menjadi lebih terang saat makin ke depan, itulah yang membuat kedalaman
   terbaca di latar gelap.
7. **Bahasa Indonesia sederhana.** Tidak ada istilah teknis di antarmuka —
   "Layar PC", bukan "monitoring grid"; "Catatan aktivitas", bukan "audit log".

## Perilaku yang ikut diputuskan

- **Aksi massal selalu dikonfirmasi** dan hasilnya dilaporkan apa adanya
  ("8/10 PC berhasil", menyebut PC yang gagal).
- **Grid me-render thumbnail secara malas** (`IntersectionObserver`): hanya
  kartu yang terlihat yang meminta frame, sehingga lab 40+ PC tetap ringan dan
  agent di luar layar tidak diminta menangkap layar sama sekali.
- **Penandaan merah berarti "sedang bermasalah"**, bukan "pernah bermasalah":
  kartu disorot hanya bila ada pelanggaran dalam 2 menit terakhir. Memakai
  jumlah kumulatif membuat seluruh grid merah di pertengahan sesi dan
  menghilangkan artinya.
- **Toast dibatasi 4** dan pelanggaran berulang dari PC+subjek yang sama
  dideduplikasi di server, agar ledakan pelanggaran tidak menutupi layar.

## Konsekuensi

- Tidak ada framework CSS; token dan komponen ditulis sendiri (~24 KB CSS).
  Ini disengaja: dashboard berjalan offline di lab tanpa CDN.
- Perubahan visual harus lewat token, bukan nilai lepas di komponen.
