# Performa Agent — Pengukuran

Target NFR: **CPU rata-rata < 2%** dan **RAM < 100 MB** saat idle di PC peserta.

## Cara mengukur

```powershell
python scripts/measure_agent.py [detik] [interval_detik]
# contoh: python scripts/measure_agent.py 10 1
```

Skrip menjalankan jalur panas agent yang sebenarnya dalam mode **dry-run**
(tidak ada aplikasi yang ditutup): enumerasi seluruh proses → safety gate →
pencocokan whitelist → baca judul jendela aktif → tangkap screenshot — lalu
melaporkan RSS puncak dan CPU% proses agent.

## Hasil (mesin pengembangan)

| Metrik | Hasil | Target | Status |
|--------|-------|--------|--------|
| Peak RSS | **51.1 MB** | < 100 MB | ✅ |
| Avg CPU (seluruh mesin) | **1.46%** | < 2% | ✅ |
| Avg CPU (1 core) | 11.67% | — | — |

Lingkungan: Windows 10 Pro, 8 core, Python 3.14, ~197 proses berjalan, loop
sweep **1 detik** dengan screenshot **setiap tick**.

## Catatan kejujuran

- Angka di atas adalah **kasus terburuk**, bukan idle sebenarnya: skrip
  menyapu **semua** proses dan menangkap screenshot **setiap 1 detik**. Di
  produksi:
  - Deteksi proses berbasis **event WMI** (bukan polling tiap detik); polling
    hanya fallback pada interval lebih longgar (default 3 detik).
  - Screenshot grid hanya **~1 fps** dan **berhenti total** saat dijeda atau
    tidak ada admin yang melihat (M5). Jadi beban CPU nyata saat idle lebih
    rendah dari 1.46%.
- CPU dinormalisasi ke seluruh mesin (dibagi jumlah core), sesuai interpretasi
  NFR "CPU rata-rata < 2%". Penggunaan 1-core mentah (11.67%) didominasi oleh
  kompresi screenshot.
- Pengukuran di PC lab sebenarnya (lihat [checklist uji manual](manual-test-checklist.md)
  item M7) tetap wajib, karena spesifikasi hardware lab berbeda.
