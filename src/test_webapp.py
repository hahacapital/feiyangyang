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


if __name__ == "__main__":
    tests = [
        ("ScanRequest ma_cross", test_scan_request_ma_cross_ok),
        ("ScanRequest price_above_ma", test_scan_request_price_above_ma_ok),
        ("ScanRequest foreign keys", test_scan_request_rejects_foreign_keys),
        ("ScanRequest validation", test_scan_request_validation_rules),
        ("ScanRequest conversions", test_scan_request_conversions),
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
