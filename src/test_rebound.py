"""Assert-based tests for rebound.py (run: python3 src/test_rebound.py)."""

import sys

import numpy as np
import pandas as pd

from rebound import (
    _sma, daily_returns, primary_held,
)


def make_df(closes, start="2020-01-01"):
    """Minimal OHLC frame from a close-price list (open=high=low=close)."""
    n = len(closes)
    dates = pd.bdate_range(start, periods=n)
    c = pd.Series(closes, dtype=float).values
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c}, index=dates)


def test_primary_held_price_above_ma():
    # close rises from 1..10; MA(3) lags, so close>MA after warm-up.
    df = make_df([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    held = primary_held(df, "price_above_ma", ma=3)
    # bool dtype, aligned to index, no NaN
    assert held.dtype == bool, f"expected bool dtype, got {held.dtype}"
    assert list(held.index) == list(df.index)
    assert not held.isna().any()
    # warm-up days are flat (False); first MA(3) value is at idx 2,
    # and shift(1) pushes the first True to at least idx 3.
    assert held.iloc[0] == False and held.iloc[1] == False
    # in a steady uptrend the later days are held
    assert held.iloc[-1] == True


def test_primary_held_ma_cross():
    # V-shape: falls then rises so fast MA crosses above slow MA partway.
    closes = [10, 9, 8, 7, 6, 5, 6, 8, 10, 12, 14, 16]
    df = make_df(closes)
    held = primary_held(df, "ma_cross", fast=2, slow=4)
    assert held.dtype == bool
    assert held.iloc[0] == False  # warm-up
    assert held.iloc[-1] == True  # fast > slow at the end of the up-leg


def test_primary_held_is_lag_correct():
    # held[t] must equal cond[t-1]; verify by constructing a known flip.
    closes = [1, 1, 1, 1, 5, 5, 5, 5]
    df = make_df(closes)
    close = df["close"].astype(float)
    cond = close > _sma(close, 2)
    held = primary_held(df, "price_above_ma", ma=2)
    # held is cond shifted forward by one bar
    expected = cond.shift(1, fill_value=False).astype(bool)
    assert (held.values == expected.values).all(), "held must be cond.shift(1)"


def test_primary_held_unknown_rule_raises():
    df = make_df([1, 2, 3])
    try:
        primary_held(df, "bogus")
        assert False, "expected ValueError for unknown rule"
    except ValueError:
        pass


def test_daily_returns():
    df = make_df([100, 110, 99])
    r = daily_returns(df)
    assert abs(r.iloc[0] - 0.0) < 1e-12, "first return is 0 (no prior bar)"
    assert abs(r.iloc[1] - 0.10) < 1e-12
    assert abs(r.iloc[2] - (99 / 110 - 1)) < 1e-12


def test_candidate_healthy():
    from rebound import candidate_healthy
    # falling then rising; healthy (close>MA2) only on the up-leg, lagged 1 day.
    df = make_df([10, 9, 8, 7, 8, 10, 12])
    h = candidate_healthy(df, ma=2)
    close = df["close"].astype(float)
    expected = (close > _sma(close, 2)).shift(1, fill_value=False).astype(bool)
    assert h.dtype == bool
    assert (h.values == expected.values).all()
    assert h.iloc[0] == False  # warm-up


def test_stitch():
    from rebound import stitch
    idx = pd.bdate_range("2020-01-01", periods=4)
    held = pd.Series([True, False, False, True], index=idx)
    p_ret = pd.Series([0.01, 0.02, 0.03, 0.04], index=idx)   # primary
    c_ret = pd.Series([0.10, 0.20, 0.30, 0.40], index=idx)   # candidate
    healthy = pd.Series([True, True, False, True], index=idx)

    # naked: held->primary, flat->candidate
    n = stitch(held, p_ret, c_ret, mode="naked")
    assert list(n["ret"].round(4)) == [0.01, 0.20, 0.30, 0.04]
    assert list(n["active"]) == ["primary", "candidate", "candidate", "primary"]
    # switches: day1 primary->candidate, day3 candidate->primary => 2
    assert n["switches"] == 2

    # filtered: flat & unhealthy (day index 2) -> cash (0.0)
    f = stitch(held, p_ret, c_ret, mode="filtered", cand_healthy=healthy)
    assert list(f["ret"].round(4)) == [0.01, 0.20, 0.00, 0.04]
    assert list(f["active"]) == ["primary", "candidate", "cash", "primary"]

    # cost: 10 bps charged on each switch day (days 1 and 3)
    c = stitch(held, p_ret, c_ret, mode="naked", cost_bps=10)
    assert abs(c["ret"].iloc[1] - (0.20 - 0.0010)) < 1e-9
    assert abs(c["ret"].iloc[3] - (0.04 - 0.0010)) < 1e-9
    assert abs(c["ret"].iloc[0] - 0.01) < 1e-9  # no switch on first day

    # filtered mode requires cand_healthy
    try:
        stitch(held, p_ret, c_ret, mode="filtered")
        assert False, "expected ValueError without cand_healthy"
    except ValueError:
        pass


def test_performance_metrics():
    from rebound import equity_curve, max_drawdown, performance_metrics
    # +10%, -50%, then flat -> equity 1.1, 0.55, 0.55
    ret = pd.Series([0.10, -0.50, 0.0])
    eq = equity_curve(ret)
    assert abs(eq.iloc[-1] - 0.55) < 1e-9
    # peak 1.1 -> trough 0.55 => drawdown 0.5
    assert abs(max_drawdown(eq) - 0.5) < 1e-9

    m = performance_metrics(ret)
    assert abs(m["total_return"] - (0.55 - 1.0)) < 1e-9
    assert abs(m["max_dd"] - 0.5) < 1e-9
    # cagr = 0.55 ** (252/3) - 1  (deeply negative, just check sign + finite)
    assert m["cagr"] < 0 and m["cagr"] == m["cagr"]
    assert m["volatility"] > 0
    # calmar = cagr / max_dd
    assert abs(m["calmar"] - m["cagr"] / m["max_dd"]) < 1e-9

    # zero-variance series: sharpe/calmar guarded to 0, no exception
    flat = pd.Series([0.0, 0.0, 0.0])
    mf = performance_metrics(flat)
    assert mf["sharpe"] == 0.0 and mf["calmar"] == 0.0 and mf["max_dd"] == 0.0


def test_antifragility_metrics():
    from rebound import antifragility_metrics
    idx = pd.bdate_range("2020-01-01", periods=4)
    held = pd.Series([True, False, False, True], index=idx)
    p_ret = pd.Series([0.01, 0.02, 0.03, 0.04], index=idx)
    c_ret = pd.Series([0.10, 0.05, -0.05, 0.40], index=idx)
    a = antifragility_metrics(p_ret, c_ret, held)
    assert abs(a["off_frac"] - 0.5) < 1e-9  # 2 of 4 days off
    # park_return over off days (idx 1,2): (1.05*0.95) - 1
    assert abs(a["park_return"] - (1.05 * 0.95 - 1.0)) < 1e-9
    assert -1.0 <= a["corr_off"] <= 1.0

    # all-held -> no off days -> zeros, no exception
    all_held = pd.Series([True, True], index=idx[:2])
    z = antifragility_metrics(p_ret[:2], c_ret[:2], all_held)
    assert z["off_frac"] == 0.0 and z["park_return"] == 0.0 and z["corr_off"] == 0.0


def test_evaluate_candidate():
    from rebound import primary_held, daily_returns, evaluate_candidate
    primary = make_df(list(range(1, 31)))          # 30 bars, steady uptrend
    cand = make_df([100 + i for i in range(30)])
    held = primary_held(primary, "price_above_ma", ma=3)
    p_ret = daily_returns(primary)
    row = evaluate_candidate("CAND", primary, held, p_ret, cand,
                             min_overlap=5)
    assert row is not None
    assert row["ticker"] == "CAND"
    assert row["n_bars"] == 30
    # both naked and filtered metric families present
    for k in ("total_return_n", "cagr_n", "max_dd_n", "sharpe_n",
              "volatility_n", "calmar_n", "switches_n",
              "total_return_f", "cagr_f", "max_dd_f", "calmar_f",
              "off_frac", "park_return", "corr_off"):
        assert k in row, f"missing key {k}"

    # thin sample -> None
    short_cand = make_df([100, 101, 102], start="2020-01-01")
    thin = evaluate_candidate("SHORT", primary, held, p_ret, short_cand,
                              min_overlap=252)
    assert thin is None


def test_evaluate_all():
    from rebound import primary_held, daily_returns, evaluate_all
    primary = make_df(list(range(1, 31)))
    held = primary_held(primary, "price_above_ma", ma=3)
    p_ret = daily_returns(primary)
    frames = {
        "A": make_df([100 + i for i in range(30)]),
        "B": make_df([200 - i for i in range(30)]),
        "SHORT": make_df([1, 2, 3]),  # dropped (thin)
    }
    rows = evaluate_all(primary, held, p_ret, frames, min_overlap=5)
    tickers = {r["ticker"] for r in rows}
    assert tickers == {"A", "B"}


def test_baseline_metrics():
    from rebound import baseline_metrics
    idx = pd.bdate_range("2020-01-01", periods=4)
    held = pd.Series([True, False, False, True], index=idx)
    p_ret = pd.Series([0.10, 0.10, 0.10, 0.10], index=idx)
    b = baseline_metrics(held, p_ret)
    # buy & hold compounds all four 10% days
    assert abs(b["buy_hold"]["total_return"] - (1.1 ** 4 - 1)) < 1e-9
    # primary+cash earns 10% only on the 2 held days
    assert abs(b["primary_cash"]["total_return"] - (1.1 ** 2 - 1)) < 1e-9


def test_rank_candidates():
    from rebound import rank_candidates, antifragile_score
    rows = [
        {"ticker": "DIV",  "cagr_n": 0.40, "max_dd_n": 0.20, "corr_off": -0.10, "n_bars": 3000},
        {"ticker": "COR",  "cagr_n": 0.50, "max_dd_n": 0.20, "corr_off": 0.60,  "n_bars": 3000},
        {"ticker": "RISK", "cagr_n": 0.90, "max_dd_n": 0.40, "corr_off": 0.10,  "n_bars": 3000},
        {"ticker": "NEW",  "cagr_n": 2.00, "max_dd_n": 0.10, "corr_off": 0.00,  "n_bars": 300},
    ]
    # antifragile = cagr_n * (1 - corr_off): DIV 0.44, COR 0.20, RISK 0.81, NEW 2.00
    assert abs(antifragile_score(rows[0]) - 0.44) < 1e-9
    assert abs(antifragile_score(rows[1]) - 0.20) < 1e-9

    # default: antifragile sort, no dd cap, no history filter
    r = rank_candidates(rows, top=10)
    assert [x["ticker"] for x in r] == ["NEW", "RISK", "DIV", "COR"]

    # min_history drops the short-history NEW (300 < 756)
    r2 = rank_candidates(rows, min_history_bars=756, top=10)
    assert [x["ticker"] for x in r2] == ["RISK", "DIV", "COR"]
    # antifragile demotes correlated COR below diversifier DIV despite higher cagr
    assert [x["ticker"] for x in r2][-2:] == ["DIV", "COR"]

    # optional max_dd cap drops the high-dd RISK (0.40 > 0.25)
    r3 = rank_candidates(rows, max_dd_cap=0.25, min_history_bars=756, top=10)
    assert [x["ticker"] for x in r3] == ["DIV", "COR"]

    # no cap by default keeps the high-dd RISK
    assert any(x["ticker"] == "RISK" for x in rank_candidates(rows, top=10))

    # explicit cagr sort still works
    rc = rank_candidates(rows, sort_key="cagr", top=10)
    assert [x["ticker"] for x in rc] == ["NEW", "RISK", "COR", "DIV"]

    # top cutoff
    assert len(rank_candidates(rows, top=1)) == 1


def test_render_table():
    from rebound import render_table
    baselines = {
        "buy_hold": {"total_return": 1.5, "cagr": 0.2, "max_dd": 0.3,
                     "sharpe": 1.0, "volatility": 0.25, "calmar": 0.66},
        "primary_cash": {"total_return": 0.8, "cagr": 0.1, "max_dd": 0.15,
                         "sharpe": 0.9, "volatility": 0.2, "calmar": 0.66},
    }
    ranked = [{
        "ticker": "AZO", "n_bars": 3000, "off_frac": 0.4, "park_return": 0.6,
        "corr_off": -0.1, "total_return_n": 5.0, "cagr_n": 0.3, "max_dd_n": 0.2,
        "sharpe_n": 1.2, "volatility_n": 0.3, "calmar_n": 1.5, "switches_n": 40,
        "total_return_f": 4.0, "cagr_f": 0.25, "max_dd_f": 0.15,
        "sharpe_f": 1.1, "volatility_f": 0.28, "calmar_f": 1.6, "switches_f": 60,
    }]
    out = render_table(baselines, ranked, top=10)
    assert "AZO" in out
    assert "Buy" in out or "buy_hold" in out.lower()
    assert isinstance(out, str) and len(out) > 0


def test_current_recommendation():
    from rebound import current_recommendation
    idx = pd.bdate_range("2020-01-01", periods=3)
    ranked = [{"ticker": "GLD"}]
    flat = pd.Series([True, True, False], index=idx)
    rec = current_recommendation(flat, ranked)
    assert "FLAT" in rec and "GLD" in rec
    held = pd.Series([False, False, True], index=idx)
    rec2 = current_recommendation(held, ranked)
    assert "IN-MARKET" in rec2


def test_plot_curves(tmp_path_str="/tmp/rebound_test_plot.png"):
    import os
    from rebound import primary_held, daily_returns, plot_curves
    primary = make_df(list(range(1, 61)))           # 60 bars
    held = primary_held(primary, "price_above_ma", ma=3)
    p_ret = daily_returns(primary)
    frames = {
        "A": make_df([100 + i for i in range(60)]),
        "B": make_df([100 + 2 * i for i in range(60)]),
    }
    ranked = [
        {"ticker": "A"}, {"ticker": "B"},
    ]
    if os.path.exists(tmp_path_str):
        os.remove(tmp_path_str)
    out = plot_curves(primary, held, p_ret, ranked, frames,
                      mode="naked", top_k=2, out_path=tmp_path_str,
                      title="TEST")
    assert out == tmp_path_str
    assert os.path.exists(tmp_path_str) and os.path.getsize(tmp_path_str) > 0
    os.remove(tmp_path_str)


def test_no_lookahead_future_invariance():
    """Mutating the LAST bar must not change any past held flag or past return.

    held[t] is decided on close[t-1], so a change to the final close can only
    ever affect a (non-existent) future held flag — held is fully invariant.
    daily_returns and the stitched portfolio return may change ONLY at the
    final index. This is the no-look-ahead guarantee.
    """
    from rebound import primary_held, daily_returns, stitch, candidate_healthy
    base = [10, 11, 9, 12, 13, 8, 14, 15, 16, 9, 18, 20]
    cand = [50, 51, 52, 49, 55, 60, 58, 62, 61, 70, 72, 75]
    df_a = make_df(base)
    df_b = make_df(base[:-1] + [999.0])          # only the last close differs
    cand_a = make_df(cand)
    cand_b = make_df(cand[:-1] + [1.0])           # only the last close differs

    held_a = primary_held(df_a, "ma_cross", fast=2, slow=4)
    held_b = primary_held(df_b, "ma_cross", fast=2, slow=4)
    # held depends only on past closes -> entirely unchanged by the last bar
    assert (held_a.values == held_b.values).all(), "held leaked future data"

    ra = daily_returns(df_a)
    rb = daily_returns(df_b)
    assert (ra.values[:-1] == rb.values[:-1]).all(), "past returns changed"
    assert ra.values[-1] != rb.values[-1], "last return should change"

    # stitched portfolio: only the final day may differ
    pa, pb = daily_returns(df_a), daily_returns(df_b)
    ca, cb = daily_returns(cand_a), daily_returns(cand_b)
    ha = candidate_healthy(cand_a, ma=2)
    hb = candidate_healthy(cand_b, ma=2)
    sa = stitch(held_a, pa, ca, mode="filtered", cand_healthy=ha)["ret"]
    sb = stitch(held_b, pb, cb, mode="filtered", cand_healthy=hb)["ret"]
    assert (sa.values[:-1] == sb.values[:-1]).all(), "past portfolio returns leaked"


def test_build_parser():
    from rebound import build_parser
    p = build_parser()
    args = p.parse_args(["TSLA", "--rule", "ma_cross", "--fast", "5",
                         "--slow", "30", "--max-dd", "25", "--top", "10"])
    assert args.ticker == "TSLA"
    assert args.rule == "ma_cross"
    assert args.fast == 5 and args.slow == 30
    assert args.max_dd == 25.0
    assert args.top == 10
    # defaults
    d = p.parse_args(["NVDA", "--rule", "price_above_ma"])
    assert d.ma == 120 and d.sort == "antifragile" and d.plot_mode == "naked"
    assert d.max_dd is None and d.min_history == 5.0


if __name__ == "__main__":
    tests = [
        ("primary_held price_above_ma", test_primary_held_price_above_ma),
        ("primary_held ma_cross", test_primary_held_ma_cross),
        ("primary_held lag-correct", test_primary_held_is_lag_correct),
        ("primary_held unknown rule", test_primary_held_unknown_rule_raises),
        ("daily_returns", test_daily_returns),
        ("candidate_healthy", test_candidate_healthy),
        ("stitch naked/filtered/cost", test_stitch),
        ("performance metrics", test_performance_metrics),
        ("antifragility metrics", test_antifragility_metrics),
        ("evaluate_candidate + thin", test_evaluate_candidate),
        ("evaluate_all", test_evaluate_all),
        ("baselines", test_baseline_metrics),
        ("rank_candidates", test_rank_candidates),
        ("render_table", test_render_table),
        ("current_recommendation", test_current_recommendation),
        ("plot_curves", test_plot_curves),
        ("no look-ahead (future invariance)", test_no_lookahead_future_invariance),
        ("arg parser", test_build_parser),
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS: {name}")
            passed += 1
        except Exception as e:
            print(f"  FAIL: {name}: {e}")
            failed += 1
    print()
    print(f"Results: {passed} passed, {failed} failed out of {len(tests)}")
    sys.exit(1 if failed else 0)
