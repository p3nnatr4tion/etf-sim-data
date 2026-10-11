# etf-sim-data

Feed data gratis untuk **Simulator ETF Live** (VWRA, QQQ, VOO, VTI, CNDX, SCHD + kurs USD/IDR).
GitHub Actions menjalankan `scripts/fetch.py` tiap jam pada hari kerja (sekali sehari di akhir pekan)
dan meng-commit hasilnya ke `data/latest.json`.

## Cara kerja

| Data | Rantai penyedia (urut) | Aturan |
|---|---|---|
| Harga ETF | yfinance → endpoint chart Yahoo langsung → nasdaq.com (ETF AS) → Stooq (butuh secret `STOOQ_APIKEY`) → Alpha Vantage (butuh secret `ALPHAVANTAGE_KEY`) | Penyedia pertama yang lolos validasi dipakai |
| Kurs USD/IDR | open.er-api.com, currency-api (jsDelivr + Cloudflare Pages), Frankfurter, Yahoo `IDR=X` | Semua dicoba, dipakai **median**; sumber yang menyimpang >2% dibuang |

Validasi: kisaran harga wajar per ETF (menangkap salah satuan pence), perubahan harian maks ±25%,
data maks 10 hari. Bila semua penyedia gagal, nilai terakhir dipertahankan dengan `"stale": true`
sehingga data bagus tidak pernah tertimpa data buruk. Detail tiap percobaan ada di blok `status`.

## URL untuk dashboard (mirror)

1. `https://raw.githubusercontent.com/p3nnatr4tion/etf-sim-data/main/data/latest.json` (cache ±5 menit)
2. `https://p3nnatr4tion.github.io/etf-sim-data/data/latest.json` (bila GitHub Pages aktif)
3. `https://cdn.jsdelivr.net/gh/p3nnatr4tion/etf-sim-data@main/data/latest.json` (di-purge tiap update)

Laporan kesehatan semua penyedia ada di `data/health.json` dan ringkasan tiap run di tab Actions (diperbarui sekali sehari).

Riwayat harian ringkas tersimpan di `data/daily/YYYY-MM-DD.json`.

## Menjalankan lokal

```bash
pip install -r requirements.txt
python -m pytest -q tests
python scripts/fetch.py
```

Data hanya untuk edukasi, tanpa jaminan akurasi.
