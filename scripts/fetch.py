#!/usr/bin/env python3
"""
Feed data untuk Simulator ETF.

Mengambil harga 6 ETF dan kurs USD/IDR dari beberapa sumber gratis, memvalidasi,
lalu menulis data/latest.json. Prinsip:
  * Tiap jenis data punya rantai penyedia (provider chain). Penyedia pertama yang
    lolos validasi dipakai; yang gagal dicatat di blok "status".
  * Kurs diambil dari semua sumber yang hidup lalu dipakai mediannya, sumber yang
    menyimpang > 2% dari median dibuang.
  * Data lama yang valid TIDAK pernah ditimpa data buruk. Bila semua penyedia
    gagal untuk satu ETF, nilai terakhir dipertahankan dengan tanda stale=True.
  * Skrip selalu keluar dengan kode 0 kecuali tidak ada data sama sekali, supaya
    satu sumber yang down tidak mematikan workflow.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "latest.json"
DAILY_DIR = ROOT / "data" / "daily"
UA = {"User-Agent": "etf-sim-data/1.0 (+https://github.com/p3nnatr4tion/etf-sim-data)"}
TIMEOUT = 20

# ticker dashboard -> simbol tiap penyedia
ETFS = {
    "VWRA": {"yahoo": "VWRA.L", "stooq": "vwra.uk", "av": "VWRA.LON"},
    "QQQ":  {"yahoo": "QQQ",    "stooq": "qqq.us",  "av": "QQQ"},
    "VOO":  {"yahoo": "VOO",    "stooq": "voo.us",  "av": "VOO"},
    "VTI":  {"yahoo": "VTI",    "stooq": "vti.us",  "av": "VTI"},
    "CNDX": {"yahoo": "CNDX.L", "stooq": "cndx.uk", "av": "CNDX.LON"},
    "SCHD": {"yahoo": "SCHD",   "stooq": "schd.us", "av": "SCHD"},
}
# Kisaran harga wajar (USD) untuk menangkap data rusak / salah satuan (pence).
SANE = {"VWRA": (60, 600), "QQQ": (200, 2500), "VOO": (200, 2500), "VTI": (100, 1500), "CNDX": (500, 6000), "SCHD": (10, 200)}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def with_retry(fn, tries=3, base=2.0):
    """Exponential backoff sederhana untuk error jaringan/5xx/429."""
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            msg = str(e)
            fatal = any(c in msg for c in ("HTTP 400", "HTTP 401", "HTTP 403", "HTTP 404"))
            if fatal or i == tries - 1:
                break
            time.sleep(base * (2 ** i))
    raise last


def http_get(url: str, params=None) -> requests.Response:
    r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code} {url}")
    return r


# ---------------------------------------------------------------- analitik murni
def derive(dates: list[str], closes: list[float], adj: list[float] | None = None) -> dict:
    """Hitung harga, perubahan harian, performa periodik, CAGR 5 thn dari seri harian
    (tanggal ISO naik, close mentah, adj close opsional untuk total return)."""
    if len(dates) != len(closes) or len(closes) < 2:
        raise ValueError("seri harga terlalu pendek")
    adj = adj or closes
    last_d = datetime.fromisoformat(dates[-1])
    price, prev = closes[-1], closes[-2]

    def base_idx(target: datetime) -> int | None:
        # indeks terakhir yang tanggalnya <= target
        idx = None
        for i, d in enumerate(dates):
            if datetime.fromisoformat(d) <= target:
                idx = i
            else:
                break
        if idx is None:
            first = datetime.fromisoformat(dates[0])
            return 0 if (first - target).days <= 7 else None
        return idx

    def perf(target: datetime) -> float | None:
        i = base_idx(target)
        if i is None or i >= len(adj) - 1 or adj[i] <= 0:
            return None
        return round((adj[-1] / adj[i] - 1) * 100, 3)

    def months_back(n: int) -> datetime:
        y, m = last_d.year, last_d.month - n
        while m <= 0:
            m += 12
            y -= 1
        day = min(last_d.day, 28)
        return last_d.replace(year=y, month=m, day=day)

    out = {
        "price": round(price, 4),
        "prevClose": round(prev, 4),
        "chg": round((price / prev - 1) * 100, 3) if prev else None,
        "asOf": dates[-1],
        "perf": {
            "w1": perf(last_d - timedelta(days=7)),
            "m1": perf(months_back(1)),
            "m3": perf(months_back(3)),
            "ytd": perf(datetime(last_d.year - 1, 12, 31)),
            "y1": perf(months_back(12)),
            "y3": perf(months_back(36)),
            "y5": perf(months_back(60)),
        },
        "cagr5": None,
        "spark": [round(x, 4) for x in closes[-60:]],
    }
    i5 = base_idx(months_back(60))
    if i5 is not None and i5 < len(adj) - 1:
        yrs = (last_d - datetime.fromisoformat(dates[i5])).days / 365.25
        if yrs >= 4.5 and adj[i5] > 0:
            out["cagr5"] = round(((adj[-1] / adj[i5]) ** (1 / yrs) - 1) * 100, 3)
    return out


def validate_quote(t: str, q: dict, today: datetime) -> None:
    lo, hi = SANE[t]
    if not (lo <= q["price"] <= hi):
        raise ValueError(f"harga {q['price']} di luar kisaran wajar {lo}-{hi}")
    if q["chg"] is not None and abs(q["chg"]) > 25:
        raise ValueError(f"perubahan harian {q['chg']}% tidak wajar")
    age = (today.date() - datetime.fromisoformat(q["asOf"]).date()).days
    if age > 10:
        raise ValueError(f"data basi {age} hari")


def robust_fx(samples: dict[str, float]) -> tuple[float, list[str]]:
    """Median dari sumber yang hidup; buang yang menyimpang > 2%."""
    vals = {k: v for k, v in samples.items() if v and 1000 < v < 100000}
    if not vals:
        raise ValueError("tidak ada kurs valid")
    med = statistics.median(vals.values())
    keep = {k: v for k, v in vals.items() if abs(v / med - 1) <= 0.02}
    return round(statistics.median(keep.values()), 4), sorted(keep)


# ---------------------------------------------------------------- penyedia harga
def prov_yahoo(t: str) -> dict:
    import yfinance as yf  # impor malas supaya tes tidak butuh jaringan

    def go():
        tk = yf.Ticker(ETFS[t]["yahoo"])
        h = tk.history(period="6y", interval="1d", auto_adjust=False, actions=False)
        if h is None or h.empty:
            raise RuntimeError("yfinance kosong")
        cur = (getattr(tk, "history_metadata", None) or {}).get("currency")
        if cur and cur != "USD":
            raise RuntimeError(f"mata uang {cur}, bukan USD")
        h = h.dropna(subset=["Close"])
        dates = [d.strftime("%Y-%m-%d") for d in h.index]
        closes = [float(x) for x in h["Close"]]
        adj = [float(x) for x in h["Adj Close"]] if "Adj Close" in h else None
        return derive(dates, closes, adj)
    return with_retry(go)


def prov_stooq(t: str) -> dict:
    def go():
        params = {"s": ETFS[t]["stooq"], "i": "d"}
        key = os.environ.get("STOOQ_APIKEY")
        if key:
            params["apikey"] = key
        txt = http_get("https://stooq.com/q/d/l/", params).text
        if "Date" not in txt[:40]:
            raise RuntimeError("stooq tidak mengembalikan CSV")
        rows = list(csv.DictReader(io.StringIO(txt)))
        rows = [r for r in rows if r.get("Close") not in (None, "", "N/D")]
        start = (now_utc() - timedelta(days=6 * 366)).strftime("%Y-%m-%d")
        rows = [r for r in rows if r["Date"] >= start]
        return derive([r["Date"] for r in rows], [float(r["Close"]) for r in rows])  # stooq: harga sudah disesuaikan split
    return with_retry(go)


def prov_alphavantage(t: str) -> dict:
    key = os.environ.get("ALPHAVANTAGE_KEY")
    if not key:
        raise RuntimeError("ALPHAVANTAGE_KEY tidak diisi (dilewati)")

    def go():
        j = http_get("https://www.alphavantage.co/query", {"function": "TIME_SERIES_WEEKLY_ADJUSTED", "symbol": ETFS[t]["av"], "apikey": key}).json()
        ts = j.get("Weekly Adjusted Time Series")
        if not ts:
            raise RuntimeError(j.get("Note") or j.get("Information") or j.get("Error Message") or "respons AV kosong")
        ks = sorted(ts)
        return derive(ks, [float(ts[k]["4. close"]) for k in ks], [float(ts[k]["5. adjusted close"]) for k in ks])
    return with_retry(go, tries=2)


PRICE_PROVIDERS = [("yahoo", prov_yahoo), ("stooq", prov_stooq), ("alphavantage", prov_alphavantage)]


# ---------------------------------------------------------------- penyedia kurs
def fx_erapi() -> float:
    j = http_get("https://open.er-api.com/v6/latest/USD").json()
    if j.get("result") != "success":
        raise RuntimeError(j.get("error-type", "gagal"))
    return float(j["rates"]["IDR"])


def fx_fawaz() -> float:
    for u in ("https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.min.json",
              "https://latest.currency-api.pages.dev/v1/currencies/usd.min.json"):
        try:
            return float(http_get(u).json()["usd"]["idr"])
        except Exception:  # noqa: BLE001
            continue
    raise RuntimeError("currency-api tidak merespons di dua mirror")


def fx_frankfurter() -> float:
    return float(http_get("https://api.frankfurter.dev/v1/latest", {"from": "USD", "to": "IDR"}).json()["rates"]["IDR"])


def fx_yahoo() -> float:
    import yfinance as yf
    h = yf.Ticker("IDR=X").history(period="5d")
    return float(h["Close"].dropna().iloc[-1])


FX_PROVIDERS = [("er-api", fx_erapi), ("currency-api", fx_fawaz), ("frankfurter", fx_frankfurter), ("yahoo", fx_yahoo)]


# ---------------------------------------------------------------- main
def load_prev() -> dict:
    try:
        return json.loads(OUT.read_text())
    except Exception:  # noqa: BLE001
        return {}


def main() -> int:
    t0 = now_utc()
    prev = load_prev()
    status: dict[str, dict] = {}
    etfs: dict[str, dict] = {}

    for t in ETFS:
        tried = []
        for name, fn in PRICE_PROVIDERS:
            try:
                q = fn(t)
                validate_quote(t, q, t0)
                q.update(src=name, stale=False)
                etfs[t] = q
                tried.append({"p": name, "ok": True})
                break
            except Exception as e:  # noqa: BLE001
                tried.append({"p": name, "ok": False, "err": str(e)[:160]})
        if t not in etfs and t in prev.get("etfs", {}):
            etfs[t] = dict(prev["etfs"][t], stale=True)
        status[t] = {"tried": tried}
        print(t, "->", etfs.get(t, {}).get("src", "GAGAL"), etfs.get(t, {}).get("price"), file=sys.stderr)

    samples, fx_tried = {}, []
    for name, fn in FX_PROVIDERS:
        try:
            samples[name] = fn()
            fx_tried.append({"p": name, "ok": True, "v": round(samples[name], 2)})
        except Exception as e:  # noqa: BLE001
            fx_tried.append({"p": name, "ok": False, "err": str(e)[:160]})
    try:
        rate, used = robust_fx(samples)
        fx = {"rate": rate, "sources": used, "asOf": iso(t0), "stale": False}
    except ValueError:
        fx = dict(prev.get("fx", {}), stale=True) if prev.get("fx") else None
    status["fx"] = {"tried": fx_tried}

    if not etfs and not fx:
        print("Tidak ada data sama sekali, latest.json tidak diubah.", file=sys.stderr)
        return 1

    doc = {"schema": 1, "generatedAt": iso(t0), "fx": fx, "etfs": etfs, "status": status}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    slim = {"date": t0.strftime("%Y-%m-%d"), "fx": fx and fx.get("rate"),
            "close": {k: v["price"] for k, v in etfs.items()}}
    (DAILY_DIR / f"{slim['date']}.json").write_text(json.dumps(slim, separators=(",", ":")))
    print(json.dumps({"ok": list(etfs), "fx": fx and fx.get("rate")}), file=sys.stderr)
    return 0


def probe() -> int:
    """Uji SEMUA penyedia (bukan hanya sampai yang pertama berhasil) lalu tulis
    laporan kesehatan ke data/health.json dan ringkasan Markdown ke stdout."""
    t0 = now_utc()
    rows, report = [], {"checkedAt": iso(t0), "price": {}, "fx": {}}
    for t in ETFS:
        report["price"][t] = {}
        for name, fn in PRICE_PROVIDERS:
            s = time.time()
            try:
                q = fn(t)
                validate_quote(t, q, t0)
                r = {"ok": True, "price": q["price"], "asOf": q["asOf"]}
            except Exception as e:  # noqa: BLE001
                r = {"ok": False, "err": str(e)[:120]}
            r["ms"] = int((time.time() - s) * 1000)
            report["price"][t][name] = r
            rows.append(f"| {t} | {name} | {'OK' if r['ok'] else 'GAGAL'} | {r.get('price', '')} | {r['ms']} | {r.get('err', '')} |")
    for name, fn in FX_PROVIDERS:
        s = time.time()
        try:
            r = {"ok": True, "rate": round(fn(), 2)}
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "err": str(e)[:120]}
        r["ms"] = int((time.time() - s) * 1000)
        report["fx"][name] = r
        rows.append(f"| USD/IDR | {name} | {'OK' if r['ok'] else 'GAGAL'} | {r.get('rate', '')} | {r['ms']} | {r.get('err', '')} |")
    (ROOT / "data" / "health.json").write_text(json.dumps(report, separators=(",", ":")))
    print("| Data | Penyedia | Status | Nilai | ms | Catatan |\n|---|---|---|---|---|---|\n" + "\n".join(rows))
    return 0


if __name__ == "__main__":
    sys.exit(probe() if "--probe" in sys.argv else main())
