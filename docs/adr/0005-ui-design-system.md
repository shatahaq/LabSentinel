# ADR-0005: Design system UI dashboard

**Status:** Diterima · **Tanggal:** 2026-10-03 · **Direvisi:** 2026-10-06

## Konteks

Dashboard dipakai dosen dan laboran yang **bukan orang teknis**, sering di
proyektor atau layar lab 1366×768, sambil mengawasi puluhan PC. Keputusan
visual di sini menentukan apakah pengawas bisa menangkap keadaan lab dalam
sekali lihat.

Versi pertama (sidebar putih, satu aksen biru, kartu statistik bertepi warna,
badge berbentuk pil di setiap baris) berfungsi, tetapi terasa seperti templat
admin generik dan teksnya banyak mengulang judul. Revisi 2026-10-06 mengganti
tampilannya tanpa mengubah perilaku.

## Keputusan

Satu design system dengan token terpusat ([tokens.css](../../dashboard/web/css/tokens.css)),
berangkat dari **meja pengawas di lab komputer**:

1. **Dua bahan.** *Kertas* untuk semua yang dibaca dan diputuskan pengawas
   (bagian bergaris seperti formulir, tabel bergaya buku register), dan
   *dinding monitor* gelap untuk layar peserta. Grid Layar PC meniru
   multiviewer ruang kontrol siaran: gambar di bingkai gelap, label nama di
   bawahnya, dan **tepi merah = PC itu sedang melanggar** (seperti lampu
   tally). Bingkai tetap gelap di tema terang maupun gelap.
2. **Pita tinta di atas, baris sesi di bawahnya.** Navigasi berupa tab teks
   dengan angka pintasan `1`–`5` di pita gelap; baris tipis di bawahnya selalu
   menyebut sesi yang berjalan, sehingga konteks itu terlihat di setiap
   halaman.
3. **Tindakan berwarna tinta, bukan warna merek.** Tombol utama hitam (putih
   di tema gelap). Warna semantik (hijau/kuning/merah) **hanya** untuk status
   mesin; biru hanya untuk fokus keyboard dan PC yang sedang dipilih.
4. **Status tidak pernah hanya warna.** Setiap status membawa bentuk berbeda
   *dan* kata: "Aktif" ●, "Dijeda" ‖, "Terputus" ⚠, "Mati" ○. Tetap terbaca di
   proyektor pucat dan cetakan hitam-putih. Tidak ada badge pil; anotasi
   seperti "dicatat saja" ditulis sebagai teks kecil.
5. **Tipografi dibundel, bukan dari CDN.** Public Sans untuk teks dan IBM Plex
   Mono untuk hal yang dibaca mesin (nama PC, jam, kode pemasangan, nama
   berkas, domain). Berkas `woff2` ada di `dashboard/web/fonts` (lisensi SIL
   OFL) karena lab sering tanpa internet. Sudut hampir siku (2–4px).
6. **Detail khas subjek, bukan hiasan.** Denah lab di Beranda (satu kotak per
   PC, dikelompokkan per baris meja dari namanya) menggantikan kartu angka;
   kode pemasangan ditulis per kotak seperti nomor peserta di lembar jawaban.
7. **Ikon SVG inline, bukan emoji**, dan hanya bila menambah arti. State
   kosong berupa kalimat rata kiri, tanpa ikon besar di tengah.
8. **Mode gelap dirancang terpisah**, bukan hasil pembalikan warna: kertas
   menjadi grafit dan makin terang saat makin ke depan; pita dan dinding
   monitor tetap paling gelap.
9. **Bahasa Indonesia sederhana, dari sisi pengawas.** Kalimat pendek dan
   aktif; tidak mengulang judul di subjudul; tidak ada istilah teknis —
   "Layar PC", bukan "monitoring grid"; "Catatan aktivitas", bukan "audit log".
   Teks tidak boleh menjanjikan lebih dari yang terjadi (mis. pelanggaran yang
   hanya dicatat tidak disebut "ditindak").

## Perilaku yang ikut diputuskan

- **Aksi massal selalu dikonfirmasi** dan hasilnya dilaporkan apa adanya
  ("8/10 PC berhasil", menyebut PC yang gagal).
- **Grid hanya meminta frame untuk kartu yang terlihat**
  (`IntersectionObserver`), sehingga lab 40+ PC tetap ringan.
- **Penandaan merah berarti "sedang bermasalah"**, bukan "pernah bermasalah":
  kartu dan kotak denah disorot hanya bila ada pelanggaran dalam 2 menit
  terakhir. Jumlah kumulatif tetap ditampilkan sebagai angka.
- **Toast dibatasi 4** dan pelanggaran berulang dari PC+subjek yang sama
  dideduplikasi di server, agar ledakan pelanggaran tidak menutupi layar.

## Konsekuensi

- Tidak ada framework CSS; token dan komponen ditulis sendiri. Font menambah
  sekitar 72 KB ke paket dashboard.
- Perubahan visual harus lewat token, bukan nilai lepas di komponen.
- Selektor yang dipakai CI (`.pc`, `#pc-grid`, `.pc__screen img`, `.dialog`,
  `.dialog__foot .btn--primary`) dipertahankan.
