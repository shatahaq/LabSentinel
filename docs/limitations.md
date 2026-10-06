# Batasan yang diketahui

Daftar jujur tentang apa yang **tidak** dilakukan Lab Sentinel, apa yang bisa
salah, dan apa yang **belum terverifikasi**. Perbarui daftar ini setiap kali
salah satunya berubah.

## A. Belum terverifikasi di Windows sungguhan

Pengembangan putaran M6–M7 dilakukan di Linux. Semua logika diuji otomatis,
tetapi bagian berikut **hanya** bisa dibuktikan di PC Windows (sebagian sebagai
Administrator) lewat [manual-test-checklist.md](manual-test-checklist.md):

- Layanan Windows (pywin32): install, recovery actions, stop/shutdown, Event Log.
- Desktop worker: `CreateProcessAsUser` ke sesi pengguna, lalu tangkapan layar,
  `EnumWindows`, judul jendela, `WM_CLOSE`, dan kunci layar **sebagai SYSTEM di
  sesi pengguna**. Ini jalur utama penegakan saat terpasang sebagai layanan.
- Menyalakan ulang helper tray sebagai pengguna (`WTSQueryUserToken`).
- Kebijakan browser di `HKLM` dan reaksi Chrome/Edge asli (lihat M4 di checklist).
- ACL token (`icacls` dengan SID), aturan firewall, kunci `Run`.
- Build PyInstaller **di Windows** dan kedua installer Inno Setup (belum pernah
  dikompilasi). Build PyInstaller di Linux sudah berhasil dan berjalan.
- Laporan dibuka di Adobe Reader / Microsoft Excel.
- Performa dengan 40+ PC di jaringan lab nyata (yang terukur: satu PC
  pengembangan, lihat [performance.md](performance.md); 14 PC simulasi di satu
  mesin).

## B. Platform dan cakupan

- **Hanya Windows 10/11 64-bit** untuk agent. Dashboard juga ditujukan untuk
  Windows (berjalan di Linux untuk pengembangan).
- **Pemblokiran situs hanya untuk Chrome dan Edge** (kebijakan Chromium). Browser
  lain tidak diblokir situsnya; whitelist aplikasi yang menahannya (browser yang
  tidak diizinkan akan ditutup).
- **Hanya sesi konsol** yang dipantau. Sesi Remote Desktop tidak.
- **Bekukan input bisa dilepas peserta dengan Ctrl+Alt+Del.** Pembekuan
  (`BlockInput`) sengaja dibuat demikian oleh Windows sebagai pengaman, jadi
  ini penahan sementara saat pengawas mendekat, bukan kunci yang tak bisa
  ditembus. Juga butuh desktop worker aktif di sesi peserta (sama seperti
  tangkapan layar); bila "layar tidak terjangkau", membekukan tidak tersedia.
- **Hanya monitor utama** yang ditangkap layarnya. Isi di monitor kedua tidak
  terlihat di dashboard maupun bukti.
- **Satu dashboard, satu LAN.** Agent menemukan dashboard lewat broadcast UDP;
  lintas subnet/VLAN perlu `direct_url`. Tidak ada beberapa dashboard
  berbagi data.
- **Satu peran admin.** Semua tindakan tercatat atas nama "pengawas"; dashboard
  tidak punya akun per pengawas, jadi catatan aktivitas tidak membedakan
  *siapa* di antara beberapa pengawas yang bertindak. UI hanya di `localhost`
  PC pengawas.

## C. Ketepatan deteksi

- **Situs dari judul jendela** kurang presisi: bisa terlewat (judul tidak
  memuat nama situs) atau keliru (judul kebetulan memuat kata kunci). Ditandai
  "dari judul jendela" dan disertai bukti; laporan mengingatkan untuk memeriksa
  bukti. Pagar utama tetap whitelist aplikasi
  ([ADR-0003](adr/0003-site-blocking-via-policy.md)).
- **Deteksi aplikasi memakai polling tiap 3 detik** (bukan event WMI; sumber
  WMI ada di kode tetapi belum disambungkan). Aplikasi bisa terbuka hingga
  ~3 detik sebelum terdeteksi, ditambah jeda peringatan sebelum ditutup.
  *Safety gate* hanya menindak proses yang jendelanya terlihat; aplikasi yang
  jendelanya muncul belakangan diperiksa lagi saat jendela itu muncul.
- **Aplikasi yang berjalan dengan hak administrator tidak pernah ditindak**
  (installer, uninstaller, alat admin). Peserta tanpa hak admin tidak bisa
  memulainya. Kalau akun peserta ternyata administrator, pengawasan bisa
  dilewati dengan "Run as administrator", jadi akun peserta **harus** akun
  standar.
- **Pelanggaran saat agent tidak terhubung tidak diantrikan.** Penegakan tetap
  berjalan dengan aturan tersimpan, tetapi catatannya hilang (kecuali laporan
  "agent dihentikan tidak wajar", yang dikirim begitu tersambung).
- **"Agent dihentikan tidak wajar"** tidak bisa membedakan penghentian oleh
  admin untuk perawatan dari perbuatan curang; detailnya hanya menyebut cara
  berhentinya (paksa saat PC menyala / listrik padam / layanan dihentikan
  manual / watchdog). Bila agent dihapus sama sekali, dashboard hanya
  menampilkan PC itu "Terputus"/"Mati".
- Waktu di laporan memakai jam PC pengawas; jam PC lab yang tidak sinkron
  memengaruhi kolom "Waktu" pelanggaran.

## D. Keamanan

- **Tanpa TLS.** Koneksi agent↔dashboard (`ws://`) tidak dienkripsi. Seseorang
  yang dapat menyadap lalu lintas segmen LAN yang sama (port mirroring, ARP
  spoofing) dapat melihat tangkapan layar dan mencuri token PC untuk menyamar
  sebagai PC tersebut. Mitigasi: lab di VLAN tersendiri, switch terkelola,
  `pendaftaran PC baru` ditutup setelah pemasangan. Beacon sudah membawa flag
  `tls`, tetapi server dan client belum mengimplementasikannya.
- **Kode pairing 6 digit** dibatasi 5 salah per alamat sumber per 15 menit.
  Penyerang yang bisa berganti banyak alamat IP dapat mencoba lebih cepat —
  karena itu tutup pendaftaran begitu semua PC terpasang, atau buat kode baru.
- **Akun peserta wajib non-administrator.** Administrator lokal bisa
  menghentikan/menghapus layanan, mengubah kebijakan, atau membaca token. Lab
  Sentinel hanya *melaporkan* hal itu (run marker), tidak bisa mencegahnya.
- **Disk image yang di-clone setelah pairing** membuat beberapa PC berbagi
  identitas dan token; mereka saling memutus di dashboard. Clone sebelum
  pairing, atau hapus `agent_id` + `token` lalu pairing ulang di tiap PC (lihat
  [deploy-guide.md](deploy-guide.md)).
- Helper tray memakai token yang bisa dibaca pengguna; ia sengaja tidak punya
  wewenang apa pun, tetapi peserta dapat menyembunyikan ikonnya (dilaporkan ke
  dashboard dan dinyalakan ulang oleh layanan).

## E. Operasional

- **Penjadwalan sesi belum ada di UI.** Dashboard menjalankan sesi terjadwal
  (mulai/akhir otomatis, tercatat bila terlewat) dan menampilkan daftar
  "Terjadwal", tetapi belum ada formulir untuk membuatnya.

- Installer belum ditandatangani (code signing) → Windows SmartScreen akan
  memperingatkan, dan antivirus **bisa** salah mendeteksinya (belum diuji di
  antivirus mana pun). Atasi dengan pengecualian folder oleh admin
  ([deploy-guide](deploy-guide.md#antivirus-dan-smartscreen-lakukan-sebelum-memasang));
  solusi permanennya code signing. Teks tombol wizard Inno Setup berbahasa Inggris (pesan Lab
  Sentinel berbahasa Indonesia); tambahkan `Indonesian.isl` bila perlu.
- PDF menyertakan paling banyak 30 bukti tangkapan layar; sisanya bisa dibuka di
  dashboard (laporan menyebutkannya).
- Bila Chrome tidak terpasang di PC pengawas, UI dibuka di Edge; bila keduanya
  tidak ada, di browser bawaan (tab biasa, bukan jendela aplikasi).
- Dashboard yang ditutup jendelanya tetap mencatat bila dijalankan dengan
  `--keep-running` (pintasan installer sudah memakainya); menutup **konsol**
  dashboard menghentikannya.
