"""Assert-based tests for the webapp layer (run: python3 src/test_webapp.py).

Mirrors test_rebound.py's hand-rolled runner. Needs the deps from requirements.txt
but NO OHLC cache (synthetic in-memory frames + injected primary loader)."""

import sys

import pandas as pd
from pydantic import ValidationError

from webapp.schemas import ScanRequest


def make_df(closes, start="2018-01-01"):
    n = len(closes)
    dates = pd.bdate_range(start, periods=n)
    c = pd.Series(closes, dtype=float).values
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c}, index=dates)


def test_scan_request_ma_cross_ok():
    r = ScanRequest(ticker="TSLA", rule="ma_cross", fast=5, slow=30)
    assert r.params() == {"fast": 5, "slow": 30}
    assert r.mode == "naked" and r.sort == "antifragile" and r.min_history == 5.0


def test_scan_request_price_above_ma_ok():
    r = ScanRequest(ticker="NVDA", rule="price_above_ma", ma=120)
    assert r.params() == {"ma": 120}


def test_scan_request_rejects_foreign_keys():
    # ma_cross must not also carry ma (silent-ignore bug guard)
    try:
        ScanRequest(ticker="X", rule="ma_cross", fast=5, slow=30, ma=120)
        assert False, "expected ValidationError for foreign key 'ma'"
    except ValidationError:
        pass
    # price_above_ma must not carry fast/slow
    try:
        ScanRequest(ticker="X", rule="price_above_ma", ma=120, fast=5)
        assert False, "expected ValidationError for foreign key 'fast'"
    except ValidationError:
        pass


def test_scan_request_validation_rules():
    for bad in (
        dict(ticker="X", rule="ma_cross", fast=30, slow=5),   # fast !< slow
        dict(ticker="X", rule="ma_cross", fast=1, slow=30),   # fast < 2
        dict(ticker="X", rule="price_above_ma", ma=1),        # ma < 2
        dict(ticker="X", rule="bogus", ma=10),                # unknown rule
        dict(ticker="X", rule="ma_cross", fast=5, slow=30, sort="nope"),
        dict(ticker="X", rule="ma_cross", fast=5, slow=30, mode="weird"),
    ):
        try:
            ScanRequest(**bad)
            assert False, f"expected ValidationError for {bad}"
        except ValidationError:
            pass


def test_scan_request_conversions():
    r = ScanRequest(ticker="X", rule="ma_cross", fast=5, slow=30, max_dd=25, min_history=3)
    assert abs(r.max_dd_cap() - 0.25) < 1e-12
    assert r.min_history_bars() == int(3 * 252)
    assert ScanRequest(ticker="X", rule="ma_cross", fast=5, slow=30).max_dd_cap() is None


def _fixture():
    import webapp.engine_service as es
    primary = make_df(list(range(1, 400)))                       # long uptrend
    frames = {
        "GLD": make_df([100 + (i % 7) for i in range(400)]),     # choppy, low corr
        "QQQ": make_df([50 + i * 0.5 for i in range(400)]),      # trending
        "SHORT": make_df([1, 2, 3]),                             # thin -> skipped
        "TSLA": make_df(list(range(1, 400))),                    # == primary (must be excluded)
    }
    es.warm_load(frames)
    es.set_primary_loader(lambda t: primary if t == "TSLA" else (_ for _ in ()).throw(
        es.PrimaryNotFound(t)))
    return es


def test_run_scan_excludes_primary_and_loads_on_demand():
    es = _fixture()
    # min_history=0 so the 399-bar fixture candidates survive the history filter
    # (the 5.0-year default = 1260 bars would drop them all -> empty ranked).
    req = ScanRequest(ticker="TSLA", rule="price_above_ma", ma=20, top=10, top_k=2, min_history=0)
    out = es.run_scan(req)
    tickers = {r["ticker"] for r in out["ranked"]}
    assert "TSLA" not in tickers, "primary must be excluded from candidates"
    assert tickers <= {"GLD", "QQQ"}, "thin/primary candidates dropped"
    assert out["recommendation"]["state"] in {"in-market", "flat"}
    assert "afscore" in out["ranked"][0]                         # afscore attached for the table


def test_run_scan_primary_not_in_cache():
    es = _fixture()
    req = ScanRequest(ticker="NOPE", rule="price_above_ma", ma=20)
    try:
        es.run_scan(req)
        assert False, "expected PrimaryNotFound"
    except es.PrimaryNotFound:
        pass


def test_run_scan_progress_and_curves():
    es = _fixture()
    seen = []
    req = ScanRequest(ticker="TSLA", rule="price_above_ma", ma=20, top_k=2)
    out = es.run_scan(req, progress_cb=lambda d, t: seen.append((d, t)))
    assert seen and seen[-1][0] == seen[-1][1]                   # final progress hits 100%
    cs = out["curves"]
    assert cs["dates"] and len(cs["primary_buy_hold"]) == len(cs["dates"])
    assert all(s["state"] in {"primary", "candidate", "cash"} for s in cs["regime"])


def test_run_scan_cancellation():
    import threading
    es = _fixture()
    ev = threading.Event(); ev.set()                            # already cancelled
    req = ScanRequest(ticker="TSLA", rule="price_above_ma", ma=20)
    try:
        es.run_scan(req, cancel_event=ev)
        assert False, "expected ScanCancelled"
    except es.ScanCancelled:
        pass


def test_dev_fixture_loads():
    import webapp.engine_service as es
    es.load_dev_fixture()
    assert es.warm_count() >= 3 and "SPY" in es.universe()
    out = es.run_scan(ScanRequest(ticker="DEMO", rule="ma_cross", fast=5, slow=20, top_k=2))
    assert out["curves"]["dates"]


if __name__ == "__main__":
    tests = [
        ("ScanRequest ma_cross", test_scan_request_ma_cross_ok),
        ("ScanRequest price_above_ma", test_scan_request_price_above_ma_ok),
        ("ScanRequest foreign keys", test_scan_request_rejects_foreign_keys),
        ("ScanRequest validation", test_scan_request_validation_rules),
        ("ScanRequest conversions", test_scan_request_conversions),
        ("run_scan excludes primary", test_run_scan_excludes_primary_and_loads_on_demand),
        ("run_scan primary missing", test_run_scan_primary_not_in_cache),
        ("run_scan progress/curves", test_run_scan_progress_and_curves),
        ("run_scan cancellation", test_run_scan_cancellation),
        ("dev fixture", test_dev_fixture_loads),
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn(); print(f"  PASS: {name}"); passed += 1
        except Exception as e:
            print(f"  FAIL: {name}: {e}"); failed += 1
    print()
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(1 if failed else 0)
