import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import fetch  # noqa: E402


def series(days=6 * 365, start=100.0, cagr=0.10):
    d0 = date(2026, 10, 9) - timedelta(days=days)
    dates, closes = [], []
    g = (1 + cagr) ** (1 / 365.25)
    for i in range(days + 1):
        d = d0 + timedelta(days=i)
        if d.weekday() < 5:
            dates.append(d.isoformat())
            closes.append(start * g ** i)
    return dates, closes


def test_derive_cagr_and_perf():
    d, c = series()
    q = fetch.derive(d, c)
    assert abs(q["cagr5"] - 10.0) < 0.1
    assert abs(q["perf"]["y1"] - 10.0) < 0.3
    assert q["asOf"] == "2026-10-09"
    assert len(q["spark"]) == 60
    assert q["chg"] > 0


def test_derive_uses_adj_for_total_return():
    d, c = series(cagr=0.0)
    adj = [x * (1.03 ** (i / 252)) for i, x in enumerate(c)]
    q = fetch.derive(d, c, adj)
    assert q["perf"]["y1"] > 2.5  # dividen ikut dihitung
    assert abs(q["price"] - c[-1]) < 1e-6  # harga tetap harga mentah


def test_derive_short_series_no_y5():
    d, c = series(days=400)
    q = fetch.derive(d, c)
    assert q["perf"]["y5"] is None and q["cagr5"] is None and q["perf"]["y1"] is not None


def test_validate_rejects_pence_and_stale():
    now = datetime(2026, 10, 11, tzinfo=timezone.utc)
    ok = {"price": 193.5, "chg": 0.5, "asOf": "2026-10-09"}
    fetch.validate_quote("VWRA", ok, now)
    with pytest.raises(ValueError):
        fetch.validate_quote("VWRA", dict(ok, price=15400.0), now)  # pence
    with pytest.raises(ValueError):
        fetch.validate_quote("VWRA", dict(ok, asOf="2026-09-01"), now)
    with pytest.raises(ValueError):
        fetch.validate_quote("VOO", {"price": 715, "chg": 40, "asOf": "2026-10-09"}, now)


def test_robust_fx_drops_outlier():
    rate, used = fetch.robust_fx({"a": 17889, "b": 17881, "c": 17888, "d": 16000})
    assert "d" not in used and abs(rate - 17888) < 2
    with pytest.raises(ValueError):
        fetch.robust_fx({"a": 0, "b": 5})


def test_main_keeps_last_good(tmp_path, monkeypatch):
    out = tmp_path / "latest.json"
    out.write_text('{"etfs":{"VOO":{"price":700,"src":"yahoo"}},"fx":{"rate":17800}}')
    monkeypatch.setattr(fetch, "OUT", out)
    monkeypatch.setattr(fetch, "DAILY_DIR", tmp_path / "daily")
    def boom(*a, **k):
        raise RuntimeError("down")
    monkeypatch.setattr(fetch, "PRICE_PROVIDERS", [("x", boom)])
    monkeypatch.setattr(fetch, "FX_PROVIDERS", [("y", boom)])
    assert fetch.main() == 0
    import json
    doc = json.loads(out.read_text())
    assert doc["etfs"]["VOO"]["price"] == 700 and doc["etfs"]["VOO"]["stale"] is True
    assert doc["fx"]["rate"] == 17800 and doc["fx"]["stale"] is True
    assert doc["status"]["VOO"]["tried"][0]["ok"] is False
