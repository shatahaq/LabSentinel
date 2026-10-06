# Panduan Admin Lab Sentinel

Untuk dosen, pengawas ujian, dan laboran yang memakai dashboard sehari-hari.
Pemasangan di PC lab dibahas di [deploy-guide.md](deploy-guide.md).

## Apa yang dilakukan Lab Sentinel — dan yang tidak

**Dilakukan**, hanya selama pemantauan aktif:

- Menutup aplikasi yang tidak ada di daftar izin (setelah peringatan beberapa
  detik kepada peserta).
- Memblokir situs terlarang di Chrome dan Edge.
- Mencatat pelanggaran beserta satu tangkapan layar sebagai bukti.
- Menampilkan layar PC peserta di dashboard ketika Anda membukanya.

**Tidak pernah**: merekam ketikan keyboard, suara, atau kamera; berjalan
diam-diam. Peserta selalu melihat ikon Lab Sentinel di pojok layar yang
menunjukkan apakah pemantauan aktif atau dijeda.

## Membuka dashboard

Klik **Lab Sentinel Dashboard** di menu Start. Sebuah jendela konsol
menampilkan alamat dan **kode pemasangan**, lalu jendela dashboard terbuka.

- Menutup **jendela dashboard** tidak menghentikan pencatatan. Buka lagi di
  `http://localhost:8080`.
- Menutup **jendela konsol** menghentikan dashboard. Biarkan tetap terbuka
  (boleh diminimalkan) selama praktikum/ujian.

Halaman ada di pita gelap paling atas: **Beranda** (denah lab, pelanggaran
terbaru, sesi), **Layar PC** (grid), **Aturan**, **Laporan**, **Catatan
aktivitas**. Tombol angka `1`–`5` berpindah halaman; tombol bulan di kanan atas
mengganti tema terang/gelap. Baris tipis di bawah pita selalu menunjukkan sesi
yang berjalan (atau "Tidak ada sesi" beserta modenya).

**Denah lab** di Beranda menampilkan satu kotak per PC, dikelompokkan per baris
meja menurut namanya (`LAB-A-01`, `LAB-A-02` → baris A). Klik kotaknya untuk
membuka layar PC itu; klik angka di kanan atas denah (mis. "2 mati") untuk
membuka Layar PC yang sudah disaring.

## Alur satu sesi

1. **Sebelum sesi** — di **Aturan**, pilih mode:
   - **Ujian**: hanya Chrome dan Edge (aplikasi untuk Praktikum tidak
     berlaku); semua kategori situs dan situs tambahan diblokir.
   - **Praktikum**: browser + aplikasi yang Anda izinkan (mis. `code.exe`);
     kategori situs yang Anda centang dan situs tambahan diblokir.
   - **Bebas**: hanya memantau. Tidak ada aplikasi yang ditutup dan tidak ada
     situs yang diblokir, tetapi layar tetap terlihat dan aplikasi di luar
     daftar tetap dicatat ("hanya dicatat" di Laporan).

   Di halaman yang sama Anda bisa menambah atau menghapus:
   - **Aplikasi yang diizinkan**: ketik nama berkasnya (mis. `code.exe`,
     lihat di Task Manager → Details) lalu **Tambah**.
   - **Situs yang diblokir**: centang kategori siap pakai, dan/atau isi
     **Situs tambahan** dengan alamatnya (mis. `tiktok.com`). Alamat yang
     ditempel dari browser juga boleh; yang diblokir seluruh situsnya,
     termasuk subdomain.

   Daftar ini disimpan per pilihan, jadi berpindah mode tidak menghapusnya:
   kembali ke Praktikum, aplikasi dan kategori yang Anda pilih masih ada.
   Perubahan langsung dikirim ke semua PC yang terhubung.
2. **Mulai sesi** — di **Beranda**, klik **Mulai sesi**, beri nama (mis. "UTS
   Pemrograman Dasar - Kelas B"), pilih mode. (Sesi terjadwal yang mulai dan
   berakhir sendiri sudah didukung di sistem, tetapi belum bisa dibuat dari
   dashboard.)
3. **Selama sesi** — pantau **Layar PC**: semua PC tampil **live** dalam satu
   grid (bukan thumbnail per detik), jadi seluruh lab terlihat bergerak
   sekaligus. Klik satu PC untuk melihat layarnya lebih besar dan lebih mulus.
   (Layar yang diam tidak dikirim ulang terus-menerus, jadi jaringan tetap
   ringan; begitu layar berubah, gambarnya langsung menyusul.)
   Tombol **Jeda semua / Lanjutkan semua** di kanan atas; untuk beberapa PC
   saja, pakai **Pilih PC**. Anda juga bisa **mengunci layar** atau
   **mengirim pesan** ke PC terpilih.

   **Bekukan keyboard & mouse.** Untuk PC yang ketahuan melanggar, buka
   detail PC itu lalu **Bekukan input**: keyboard dan mouse peserta berhenti
   berfungsi sampai Anda tekan **Cairkan input**. Layar peserta tetap
   menampilkan apa yang sedang dibuka (tanpa pesan atau overlay apa pun), jadi
   Anda bisa langsung melihat yang dikerjakannya; di dashboard PC itu
   ditandai **Input dibekukan** di grid. Pembekuan otomatis lepas bila Anda
   menjeda PC itu atau agent berhenti, jadi peserta tidak akan terkunci
   permanen; peserta juga selalu bisa menekan Ctrl+Alt+Del untuk melepaskannya
   dalam keadaan darurat. Membekukan butuh **akses layar** ke PC itu (tombol
   nonaktif bila "layar tidak terjangkau").
4. **Akhiri sesi** — klik **Akhiri sesi** di Beranda.
5. **Laporan** — buka **Laporan**, pilih sesinya, klik **Unduh PDF** atau
   **Unduh Excel**. Salinan juga tersimpan otomatis di
   `C:\ProgramData\LabSentinel\dashboard\reports` (menu Start → **Folder laporan**).

### Mode uji coba

Pada pemasangan baru, **mode uji coba** aktif: pelanggaran dicatat, tetapi
aplikasi **tidak** ditutup dan situs **tidak** diblokir. Pakai mode ini untuk
mencoba aturan di satu sesi praktikum dulu. Setelah yakin, matikan di **Aturan
→ Penindakan** (atau klik **Tegakkan aturan** pada catatan kuning di
Beranda). Laporan memisahkan catatan uji coba dari yang ditegakkan.

### Jeda

**Jeda** menghentikan penegakan dan tangkapan layar di PC tersebut (misalnya
saat istirahat atau ketika Anda memang mengizinkan semua aplikasi sebentar).
Ikon peserta berubah menjadi "Dijeda". Status di dashboard adalah status yang
**dilaporkan PC itu sendiri**, bukan sekadar tombol yang terakhir ditekan;
perintah yang tidak sampai disebutkan ("8/10 PC berhasil, tidak berhasil:
LAB-A-03, LAB-A-07").

## Membaca status PC

| Tanda | Arti | Yang perlu dilakukan |
|---|---|---|
| ● **Aktif** | Terhubung dan diawasi | — |
| ‖ **Dijeda** | Pemantauan dijeda oleh pengawas | Lanjutkan bila sudah waktunya |
| ⚠ **Terputus** | Tidak mengirim kabar tanpa pamit (kabel lepas, PC hang, agent dimatikan) | Periksa PC-nya |
| ○ **Mati** | PC dimatikan dengan normal | — |
| *indikator disembunyikan* | Ikon peserta tidak tampil (ditutup/dimatikan) | Pemantauan tetap jalan; ikon dinyalakan lagi otomatis dalam ±30 detik |
| *layar tidak terjangkau* | Layanan tidak bisa melihat desktop PC itu: tidak ada tangkapan layar, judul jendela, atau penutupan aplikasi | Bila tidak pulih dalam satu menit, restart PC; laporkan ke laboran |
| Tepi merah (grid dan denah lab) | Ada pelanggaran dalam 2 menit terakhir | Lihat detailnya |

### "Agent dihentikan tidak wajar"

Muncul sebagai notifikasi merah dan baris di Laporan ketika agent di suatu PC
berhenti di luar prosedur normal. Keterangannya menyebut caranya:

- **"dihentikan paksa saat PC menyala"** — prosesnya dimatikan (butuh hak
  administrator). Paling patut dicurigai saat ujian.
- **"PC mati atau restart tanpa shutdown normal"** — tombol power ditahan,
  kabel listrik dicabut, atau listrik padam.
- **"Layanan agent dihentikan secara manual"** — seseorang menghentikan layanan
  (butuh hak administrator); bisa juga laboran yang sedang perawatan.
- **"tidak merespons dan dimulai ulang otomatis"** — gangguan teknis, bukan
  ulah peserta, tetapi ada jeda pemantauan.

## Memasang PC baru (kode pemasangan)

Di **Aturan → Pemasangan PC baru** tampil kode 6 digit. Kode ini diketik
**sekali** di jendela pemasangan yang muncul saat installer agent dijalankan di
tiap PC (atau dari menu Start → **Pasangkan PC ini ke dashboard**); setelah itu
PC memakai kunci miliknya sendiri.

- **Ganti kode**: kode lama langsung tidak berlaku. PC yang sudah terpasang
  tidak terpengaruh.
- **Tutup pendaftaran**: setelah semua PC lab terpasang, tutup
  pendaftaran agar tidak ada PC lain yang bisa ikut terhubung. Buka lagi saat
  menambah PC.
- Lima kali kode salah dari satu PC membuat PC itu harus menunggu 15 menit.

## Data, privasi, dan cadangan

- Semua data ada di PC pengawas: `C:\ProgramData\LabSentinel\dashboard\`.
  Data yang lebih tua dari 30 hari (pengaturan `retention_days`) dihapus
  otomatis, termasuk tangkapan layar bukti.
- **Cadangan**: hentikan dashboard, lalu salin seluruh folder di atas. Simpan
  laporan PDF/Excel yang dibutuhkan sebelum terhapus oleh retensi.
- **Catatan aktivitas** mencatat setiap tindakan pengawas dan tidak dapat
  diubah dari dashboard. Semua tindakan tercatat atas nama "pengawas"
  (dashboard belum membedakan akun per pengawas).

## Pemecahan masalah

| Gejala | Kemungkinan penyebab | Solusi |
|---|---|---|
| PC tidak muncul di Layar PC | Belum dipasangkan / kode salah / pendaftaran ditutup | Lihat **Catatan aktivitas** ("Kode pemasangan salah", "PC baru ditolak") |
| | Firewall memblokir | Pastikan aturan firewall installer terpasang (TCP 8765, UDP 50123) |
| | Jaringan PC pengawas berprofil **Publik** di Windows (aturan firewall installer hanya untuk profil Privat/Domain) | Ubah profil jaringan ke **Privat** (Pengaturan → Jaringan → Properti) |
| | Lab beda subnet/VLAN dengan PC pengawas | Isi alamat dashboard saat memasang agent (`ws://IP-PENGAWAS:8765`) |
| PC ditolak "perlu dipasangkan ulang" | Database dashboard diganti/dipulihkan | Di PC itu: menu Start → **Pasangkan PC ini ke dashboard** → **Pasangkan ulang** |
| "terlalu banyak percobaan, tunggu … detik" | 5 kode salah dari PC itu | Tunggu 15 menit, lalu ulangi dengan kode yang benar |
| Semua PC **Terputus** sekaligus | Dashboard berhenti / jaringan putus / IP PC pengawas berubah | Jalankan ulang dashboard; beri IP tetap pada PC pengawas |
| Jendela dashboard tidak terbuka | Tidak ada Chrome/Edge | Buka `http://localhost:8080` di browser apa saja |
| Laporan kosong | Sesi belum dimulai, atau pelanggaran terjadi di luar sesi | Pilih **Semua sesi** di Laporan untuk melihat semuanya |

Berkas log untuk dukungan teknis: dashboard di
`C:\ProgramData\LabSentinel\dashboard\logs\`, agent di
`C:\ProgramData\LabSentinel\agent\logs\`.
