# Rebound (沸羊羊 / 备胎 finder) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `src/rebound.py`, a tool that, given a primary stock and a hold/flat moving-average rule, ranks candidate "parking" assets by the stitched daily-return equity curve and prints a table + saves an equity/drawdown PNG.

**Architecture:** A new daily-return-stitching engine (distinct from the trade-based `backtest.py`). A pluggable signal layer turns a primary OHLC frame + MA rule into a lag-correct daily "held" mask; for each candidate, daily returns are stitched (primary while held, candidate/cash while flat) in two parking modes (naked, filtered), metrics are computed, candidates are filtered by a max-drawdown cap and ranked, then rendered as a stdout table + a matplotlib PNG. Reuses `data_loader` for the OHLC cache; no dependency on `backtest.py` / `indicators.py`.

**Tech Stack:** Python 3, pandas, numpy, matplotlib (new), the project's existing `data_loader` cache. Tests are assert-based (no pytest), following the `src/test_logic.py` convention.

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-25-rebound-design.md` (authoritative).
- **Module:** all code under `src/`; run scripts as `python3 src/<file>.py` from repo root.
- **Language:** all committed code, comments, docstrings, and docs in **English**.
- **Tests:** assert-based functions + a `tests = [(name, fn), ...]` runner that prints `  PASS: ...` per test and `sys.exit(1)` on any failure. Run with `python3 src/test_rebound.py`. NO pytest.
- **Two rules only:** `price_above_ma` (`--ma`, default 120) and `ma_cross` (`--fast`/`--slow`, default 5/30). jojo is intentionally excluded.
- **Two parking modes:** `naked` (park whenever flat) and `filtered` (park only when candidate `close > MA50`, else cash). Both computed for every candidate.
- **Lag-correctness:** every signal/health mask uses `.shift(1)` — day `t` decisions use only data ≤ `close[t-1]`.
- **Metric units:** all metrics stored internally as **fractions** (e.g. `max_dd = 0.416`, `cagr = 0.658`); `--max-dd` is given in percent and converted to a fraction before comparison; the table formats fractions to percentages for display.
- **Ranking is on naked-mode metrics** (`*_n`); filtered metrics are shown alongside for comparison.
- **Dependency rule:** when adding matplotlib, add it to `requirements.txt`; import it lazily inside the plot function so the rest of the module imports without it.
- **Doc rule:** the final task updates `README.md`, `README.zh.md`, and `CLAUDE.md`.
- **Cache note:** the local OHLC cache may be empty in a fresh checkout (`data/` is gitignored). All unit tests use synthetic in-memory frames; end-to-end runs require `python3 src/download_ohlc.py --init` first.

---

## File Structure

- `src/rebound.py` (create) — the entire tool: signal layer, returns/health helpers, stitching, metrics, evaluation, ranking, baselines, table, plot, CLI `main()`.
- `src/test_rebound.py` (create) — assert-based tests + runner.
- `requirements.txt` (modify) — add `matplotlib`.
- `README.md`, `README.zh.md`, `CLAUDE.md` (modify) — document the tool.

### Function inventory (final signatures — every task implements a subset of these)

```
_sma(series: pd.Series, n: int) -> pd.Series
daily_returns(df: pd.DataFrame) -> pd.Series
primary_held(df: pd.DataFrame, rule: str, *, ma=120, fast=5, slow=30) -> pd.Series  # bool
candidate_healthy(df: pd.DataFrame, ma: int = 50) -> pd.Series                       # bool
stitch(held, primary_ret, cand_ret, *, mode, cand_healthy=None, cost_bps=0.0) -> dict
    # dict keys: ret (Series), parked (bool Series), active (object Series), switches (int)
equity_curve(ret: pd.Series) -> pd.Series
max_drawdown(equity: pd.Series) -> float                  # positive fraction
performance_metrics(ret: pd.Series) -> dict               # total_return,cagr,max_dd,sharpe,volatility,calmar
antifragility_metrics(primary_ret, cand_ret, held) -> dict  # off_frac,park_return,corr_off
evaluate_candidate(ticker, primary_df, held, primary_ret, cand_df, *, cost_bps=0.0, health_ma=50, min_overlap=252) -> dict | None
evaluate_all(primary_df, held, primary_ret, candidate_frames, *, cost_bps=0.0, health_ma=50, min_overlap=252) -> list[dict]
baseline_metrics(held, primary_ret) -> dict               # {"buy_hold": {...}, "primary_cash": {...}}
rank_candidates(rows, *, sort_key="cagr", max_dd_cap=0.25, top=30) -> list[dict]
render_table(baselines, ranked, top=30) -> str
current_recommendation(held, ranked) -> str
plot_curves(primary_df, held, primary_ret, ranked, candidate_frames, *, mode="naked", top_k=3, out_path, title) -> str
scan(primary_ticker, rule, params, *, candidates=None, limit=0, cost_bps=0.0, health_ma=50, min_overlap=252, min_bars=756) -> tuple
main() -> None
```

Row dict keys produced by `evaluate_candidate` (consumed by ranking/table/plot):
`ticker, n_bars, off_frac, park_return, corr_off,
 total_return_n, cagr_n, max_dd_n, sharpe_n, volatility_n, calmar_n, switches_n,
 total_return_f, cagr_f, max_dd_f, sharpe_f, volatility_f, calmar_f, switches_f`

---

### Task 1: Module scaffold + signal layer (`_sma`, `daily_returns`, `primary_held`) + test runner

**Files:**
- Create: `src/rebound.py`
- Create: `src/test_rebound.py`

**Interfaces:**
- Consumes: nothing (foundational).
- Produces: `_sma`, `daily_returns`, `primary_held` (signatures above). `test_rebound.py` establishes the `make_df` helper and the `tests`/runner skeleton that every later task appends to.

- [ ] **Step 1: Write the failing test**

Create `src/test_rebound.py`:

```python
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
    expected = cond.shift(1).fillna(False).astype(bool)
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


if __name__ == "__main__":
    tests = [
        ("primary_held price_above_ma", test_primary_held_price_above_ma),
        ("primary_held ma_cross", test_primary_held_ma_cross),
        ("primary_held lag-correct", test_primary_held_is_lag_correct),
        ("primary_held unknown rule", test_primary_held_unknown_rule_raises),
        ("daily_returns", test_daily_returns),
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 src/test_rebound.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'rebound'` (or import error), nonzero exit.

- [ ] **Step 3: Write minimal implementation**

Create `src/rebound.py`:

```python
"""Rebound (沸羊羊 / 备胎 finder).

Given a primary stock and a hold/flat moving-average rule, find the best
"parking" asset to deploy capital into while the primary is flat. Stitches
daily returns (primary while held, candidate/cash while flat) into one equity
curve, ranks candidates, and outputs a table + an equity/drawdown PNG.

Run: python3 src/rebound.py TICKER --rule price_above_ma --ma 120
"""
from __future__ import annotations

import pandas as pd


def _sma(series: pd.Series, n: int) -> pd.Series:
    """Simple moving average."""
    return series.astype(float).rolling(n).mean()


def daily_returns(df: pd.DataFrame) -> pd.Series:
    """Close-to-close daily returns; first bar is 0 (no prior close)."""
    return df["close"].astype(float).pct_change().fillna(0.0)


def primary_held(df: pd.DataFrame, rule: str, *, ma: int = 120,
                 fast: int = 5, slow: int = 30) -> pd.Series:
    """Lag-correct daily 'exposed to primary' mask for a hold/flat rule.

    held[t] == True means the portfolio earns the primary's close[t-1]->close[t]
    return on day t. The decision uses only data on or before close[t-1]
    (enforced by .shift(1)). Warm-up / NaN -> False (flat).
    """
    close = df["close"].astype(float)
    if rule == "price_above_ma":
        cond = close > _sma(close, ma)
    elif rule == "ma_cross":
        cond = _sma(close, fast) > _sma(close, slow)
    else:
        raise ValueError(f"unknown rule: {rule!r}")
    return cond.shift(1).fillna(False).astype(bool)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 src/test_rebound.py`
Expected: `Results: 5 passed, 0 failed out of 5`, exit 0.

- [ ] **Step 5: Commit**

```bash
git add src/rebound.py src/test_rebound.py
git commit -m "feat(rebound): signal layer + returns helper + test scaffold"
```

---

### Task 2: Candidate health mask (`candidate_healthy`)

**Files:**
- Modify: `src/rebound.py`
- Modify: `src/test_rebound.py`

**Interfaces:**
- Consumes: `_sma` (Task 1).
- Produces: `candidate_healthy(df, ma=50) -> pd.Series` (bool), used by filtered-mode stitching (Task 6).

- [ ] **Step 1: Write the failing test**

Add to `src/test_rebound.py` (above the `__main__` block) and register it in the `tests` list as `("candidate_healthy", test_candidate_healthy)`:

```python
def test_candidate_healthy():
    from rebound import candidate_healthy
    # falling then rising; healthy (close>MA2) only on the up-leg, lagged 1 day.
    df = make_df([10, 9, 8, 7, 8, 10, 12])
    h = candidate_healthy(df, ma=2)
    close = df["close"].astype(float)
    expected = (close > _sma(close, 2)).shift(1).fillna(False).astype(bool)
    assert h.dtype == bool
    assert (h.values == expected.values).all()
    assert h.iloc[0] == False  # warm-up
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 src/test_rebound.py`
Expected: FAIL — `ImportError: cannot import name 'candidate_healthy'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/rebound.py` after `primary_held`:

```python
def candidate_healthy(df: pd.DataFrame, ma: int = 50) -> pd.Series:
    """Lag-correct 'candidate is healthy' mask: close > SMA(close, ma)."""
    close = df["close"].astype(float)
    cond = close > _sma(close, ma)
    return cond.shift(1).fillna(False).astype(bool)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 src/test_rebound.py`
Expected: `Results: 6 passed, 0 failed out of 6`.

- [ ] **Step 5: Commit**

```bash
git add src/rebound.py src/test_rebound.py
git commit -m "feat(rebound): candidate health mask"
```

---

### Task 3: Daily-return stitching (`stitch`)

**Files:**
- Modify: `src/rebound.py`
- Modify: `src/test_rebound.py`

**Interfaces:**
- Consumes: nothing new (operates on aligned Series).
- Produces: `stitch(held, primary_ret, cand_ret, *, mode, cand_healthy=None, cost_bps=0.0) -> dict` with keys `ret`, `parked`, `active`, `switches`. All inputs must already share one index.

- [ ] **Step 1: Write the failing test**

Add to `src/test_rebound.py` and register `("stitch naked/filtered/cost", test_stitch)`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 src/test_rebound.py`
Expected: FAIL — `ImportError: cannot import name 'stitch'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/rebound.py`:

```python
def stitch(held: pd.Series, primary_ret: pd.Series, cand_ret: pd.Series, *,
           mode: str, cand_healthy: pd.Series | None = None,
           cost_bps: float = 0.0) -> dict:
    """Stitch primary/candidate/cash daily returns into one portfolio series.

    held=True  -> earn primary_ret; flat -> earn candidate (naked) or
    candidate-if-healthy-else-cash (filtered). Inputs must share one index.
    A switch day (active asset changes vs the prior day) is charged cost_bps.
    """
    off = ~held
    if mode == "naked":
        parked = off
    elif mode == "filtered":
        if cand_healthy is None:
            raise ValueError("filtered mode requires cand_healthy")
        parked = off & cand_healthy
    else:
        raise ValueError(f"unknown mode: {mode!r}")

    ret = pd.Series(0.0, index=held.index)
    ret[held] = primary_ret[held]
    ret[parked] = cand_ret[parked]  # parked is a subset of off, so no overlap

    active = pd.Series("cash", index=held.index, dtype=object)
    active[held] = "primary"
    active[parked] = "candidate"

    changed = active != active.shift(1)
    changed.iloc[0] = False  # no switch on the first bar
    switches = int(changed.sum())
    if cost_bps:
        ret = ret - changed.astype(float) * (cost_bps / 1e4)

    return {"ret": ret, "parked": parked, "active": active, "switches": switches}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 src/test_rebound.py`
Expected: `Results: 7 passed, 0 failed out of 7`.

- [ ] **Step 5: Commit**

```bash
git add src/rebound.py src/test_rebound.py
git commit -m "feat(rebound): daily-return stitching with cost-on-switch"
```

---

### Task 4: Performance metrics (`equity_curve`, `max_drawdown`, `performance_metrics`)

**Files:**
- Modify: `src/rebound.py`
- Modify: `src/test_rebound.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `equity_curve(ret)`, `max_drawdown(equity)`, `performance_metrics(ret)` returning fractions `{total_return, cagr, max_dd, sharpe, volatility, calmar}`.

- [ ] **Step 1: Write the failing test**

Add to `src/test_rebound.py` and register `("performance metrics", test_performance_metrics)`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 src/test_rebound.py`
Expected: FAIL — `ImportError: cannot import name 'equity_curve'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/rebound.py`:

```python
def equity_curve(ret: pd.Series) -> pd.Series:
    """Cumulative equity (starting at 1.0) from a daily-return series."""
    return (1.0 + ret).cumprod()


def max_drawdown(equity: pd.Series) -> float:
    """Maximum peak-to-trough drawdown as a positive fraction (e.g. 0.42)."""
    if len(equity) == 0:
        return 0.0
    peak = equity.cummax()
    dd = (peak - equity) / peak
    return float(dd.max())


def performance_metrics(ret: pd.Series) -> dict:
    """Return-curve metrics as fractions: total_return, cagr, max_dd,
    sharpe, volatility, calmar. rf = 0; annualization factor 252."""
    n = len(ret)
    if n == 0:
        return {"total_return": 0.0, "cagr": 0.0, "max_dd": 0.0,
                "sharpe": 0.0, "volatility": 0.0, "calmar": 0.0}
    eq = equity_curve(ret)
    final = float(eq.iloc[-1])
    total_return = final - 1.0
    cagr = final ** (252.0 / n) - 1.0 if final > 0 else -1.0
    mdd = max_drawdown(eq)
    std = float(ret.std())
    sharpe = float(ret.mean() / std * (252 ** 0.5)) if std > 0 else 0.0
    volatility = std * (252 ** 0.5)
    calmar = cagr / mdd if mdd > 0 else 0.0
    return {"total_return": total_return, "cagr": cagr, "max_dd": mdd,
            "sharpe": sharpe, "volatility": volatility, "calmar": calmar}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 src/test_rebound.py`
Expected: `Results: 8 passed, 0 failed out of 8`.

- [ ] **Step 5: Commit**

```bash
git add src/rebound.py src/test_rebound.py
git commit -m "feat(rebound): performance metrics (cagr, max_dd, sharpe, calmar)"
```

---

### Task 5: Antifragility metrics (`antifragility_metrics`)

**Files:**
- Modify: `src/rebound.py`
- Modify: `src/test_rebound.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `antifragility_metrics(primary_ret, cand_ret, held) -> {off_frac, park_return, corr_off}` (all fractions); `park_return`/`corr_off` are computed over the primary's off days.

- [ ] **Step 1: Write the failing test**

Add to `src/test_rebound.py` and register `("antifragility metrics", test_antifragility_metrics)`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 src/test_rebound.py`
Expected: FAIL — `ImportError: cannot import name 'antifragility_metrics'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/rebound.py`:

```python
def antifragility_metrics(primary_ret: pd.Series, cand_ret: pd.Series,
                          held: pd.Series) -> dict:
    """Off-period reads: fraction of off days, the candidate's cumulative
    return over off days, and its return correlation with the primary over
    off days (lower/negative = more diversifying)."""
    off = ~held
    off_frac = float(off.mean()) if len(off) else 0.0
    if off.any():
        park_return = float((1.0 + cand_ret[off]).prod() - 1.0)
        a, b = primary_ret[off], cand_ret[off]
        if len(a) > 1 and a.std() > 0 and b.std() > 0:
            corr_off = float(a.corr(b))
        else:
            corr_off = 0.0
    else:
        park_return = 0.0
        corr_off = 0.0
    return {"off_frac": off_frac, "park_return": park_return, "corr_off": corr_off}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 src/test_rebound.py`
Expected: `Results: 9 passed, 0 failed out of 9`.

- [ ] **Step 5: Commit**

```bash
git add src/rebound.py src/test_rebound.py
git commit -m "feat(rebound): antifragility metrics (off_frac, park_return, corr_off)"
```

---

### Task 6: Per-candidate evaluation (`evaluate_candidate`, `evaluate_all`)

**Files:**
- Modify: `src/rebound.py`
- Modify: `src/test_rebound.py`

**Interfaces:**
- Consumes: `daily_returns`, `candidate_healthy`, `stitch`, `performance_metrics`, `antifragility_metrics`.
- Produces: `evaluate_candidate(...) -> dict | None` (the row dict; `None` if overlap `< min_overlap`); `evaluate_all(...) -> list[dict]` (skips candidates that raise).

- [ ] **Step 1: Write the failing test**

Add to `src/test_rebound.py` and register `("evaluate_candidate + thin", test_evaluate_candidate)` and `("evaluate_all", test_evaluate_all)`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 src/test_rebound.py`
Expected: FAIL — `ImportError: cannot import name 'evaluate_candidate'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/rebound.py`:

```python
def evaluate_candidate(ticker: str, primary_df: pd.DataFrame, held: pd.Series,
                       primary_ret: pd.Series, cand_df: pd.DataFrame, *,
                       cost_bps: float = 0.0, health_ma: int = 50,
                       min_overlap: int = 252) -> dict | None:
    """Evaluate one candidate over its common window with the primary.

    Returns a flat row dict with naked (_n) and filtered (_f) metric families
    plus shared antifragility reads, or None if the overlap is too short.
    """
    common = held.index.intersection(cand_df.index)
    if len(common) < min_overlap:
        return None

    cand_ret_full = daily_returns(cand_df)
    cand_healthy_full = candidate_healthy(cand_df, health_ma)

    h = held.reindex(common).fillna(False).astype(bool)
    pr = primary_ret.reindex(common).fillna(0.0)
    cr = cand_ret_full.reindex(common).fillna(0.0)
    ch = cand_healthy_full.reindex(common).fillna(False).astype(bool)

    naked = stitch(h, pr, cr, mode="naked", cost_bps=cost_bps)
    filt = stitch(h, pr, cr, mode="filtered", cand_healthy=ch, cost_bps=cost_bps)
    mn = performance_metrics(naked["ret"])
    mf = performance_metrics(filt["ret"])
    anti = antifragility_metrics(pr, cr, h)

    row = {"ticker": ticker, "n_bars": len(common), **anti}
    for k, v in mn.items():
        row[f"{k}_n"] = v
    row["switches_n"] = naked["switches"]
    for k, v in mf.items():
        row[f"{k}_f"] = v
    row["switches_f"] = filt["switches"]
    return row


def evaluate_all(primary_df: pd.DataFrame, held: pd.Series,
                 primary_ret: pd.Series, candidate_frames: dict, *,
                 cost_bps: float = 0.0, health_ma: int = 50,
                 min_overlap: int = 252) -> list:
    """Evaluate every candidate frame; skip thin/erroring ones."""
    rows = []
    for ticker, cand_df in candidate_frames.items():
        try:
            row = evaluate_candidate(ticker, primary_df, held, primary_ret,
                                     cand_df, cost_bps=cost_bps,
                                     health_ma=health_ma, min_overlap=min_overlap)
        except Exception:
            row = None
        if row is not None:
            rows.append(row)
    return rows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 src/test_rebound.py`
Expected: `Results: 11 passed, 0 failed out of 11`.

- [ ] **Step 5: Commit**

```bash
git add src/rebound.py src/test_rebound.py
git commit -m "feat(rebound): per-candidate evaluation + thin-sample skip"
```

---

### Task 7: Baselines + ranking (`baseline_metrics`, `rank_candidates`)

**Files:**
- Modify: `src/rebound.py`
- Modify: `src/test_rebound.py`

**Interfaces:**
- Consumes: `performance_metrics`; the row dicts from `evaluate_candidate`.
- Produces: `baseline_metrics(held, primary_ret) -> {"buy_hold": {...}, "primary_cash": {...}}`; `rank_candidates(rows, *, sort_key="cagr", max_dd_cap=0.25, top=30) -> list[dict]`. `SORT_KEYS` maps sort keys to the `*_n` columns.

- [ ] **Step 1: Write the failing test**

Add to `src/test_rebound.py` and register `("baselines", test_baseline_metrics)` and `("rank_candidates", test_rank_candidates)`:

```python
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
    from rebound import rank_candidates
    rows = [
        {"ticker": "HI",  "cagr_n": 0.50, "max_dd_n": 0.10},
        {"ticker": "RISK","cagr_n": 0.90, "max_dd_n": 0.40},  # exceeds cap
        {"ticker": "MID", "cagr_n": 0.30, "max_dd_n": 0.20},
    ]
    ranked = rank_candidates(rows, sort_key="cagr", max_dd_cap=0.25, top=10)
    assert [r["ticker"] for r in ranked] == ["HI", "MID"]  # RISK dropped, sorted desc
    # top cutoff
    assert len(rank_candidates(rows, max_dd_cap=1.0, top=1)) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 src/test_rebound.py`
Expected: FAIL — `ImportError: cannot import name 'baseline_metrics'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/rebound.py`:

```python
SORT_KEYS = {
    "cagr": "cagr_n",
    "total_return": "total_return_n",
    "calmar": "calmar_n",
    "sharpe": "sharpe_n",
}


def baseline_metrics(held: pd.Series, primary_ret: pd.Series) -> dict:
    """Reference curves over the primary's full window: always-long buy&hold,
    and the rule on/off with cash (0) when flat — the 'no backup' baseline."""
    buy_hold = performance_metrics(primary_ret)
    primary_cash = performance_metrics(primary_ret.where(held, 0.0))
    return {"buy_hold": buy_hold, "primary_cash": primary_cash}


def rank_candidates(rows: list, *, sort_key: str = "cagr",
                    max_dd_cap: float = 0.25, top: int = 30) -> list:
    """Drop candidates whose naked max drawdown exceeds the cap (a fraction),
    then sort by the chosen naked-mode key (descending) and take the top N."""
    col = SORT_KEYS[sort_key]
    kept = [r for r in rows if r["max_dd_n"] <= max_dd_cap]
    kept.sort(key=lambda r: r[col], reverse=True)
    return kept[:top]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 src/test_rebound.py`
Expected: `Results: 13 passed, 0 failed out of 13`.

- [ ] **Step 5: Commit**

```bash
git add src/rebound.py src/test_rebound.py
git commit -m "feat(rebound): baselines + max-dd-capped ranking"
```

---

### Task 8: Table + current recommendation (`render_table`, `current_recommendation`)

**Files:**
- Modify: `src/rebound.py`
- Modify: `src/test_rebound.py`

**Interfaces:**
- Consumes: baseline dict (Task 7), ranked row list (Task 7), `held` Series.
- Produces: `render_table(baselines, ranked, top=30) -> str`; `current_recommendation(held, ranked) -> str`.

- [ ] **Step 1: Write the failing test**

Add to `src/test_rebound.py` and register `("render_table", test_render_table)` and `("current_recommendation", test_current_recommendation)`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 src/test_rebound.py`
Expected: FAIL — `ImportError: cannot import name 'render_table'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/rebound.py` (add `import pandas as pd` already present at top):

```python
def render_table(baselines: dict, ranked: list, top: int = 30) -> str:
    """Render the baselines block + top-N candidate ranking as a string."""
    def pct(x):
        return f"{x * 100:.1f}%"

    lines = []
    lines.append("=== Baselines (primary full window) ===")
    bh, pc = baselines["buy_hold"], baselines["primary_cash"]
    lines.append(f"  Buy&Hold      total={pct(bh['total_return'])}  "
                 f"cagr={pct(bh['cagr'])}  maxDD={pct(bh['max_dd'])}  "
                 f"calmar={bh['calmar']:.2f}  sharpe={bh['sharpe']:.2f}")
    lines.append(f"  Primary+cash  total={pct(pc['total_return'])}  "
                 f"cagr={pct(pc['cagr'])}  maxDD={pct(pc['max_dd'])}  "
                 f"calmar={pc['calmar']:.2f}  sharpe={pc['sharpe']:.2f}")

    lines.append("")
    lines.append(f"=== Top {min(top, len(ranked))} backups "
                 f"(ranked by naked-mode; _n=naked, _f=filtered) ===")
    if not ranked:
        lines.append("  (no candidate cleared the max-drawdown cap)")
        return "\n".join(lines)

    display = []
    for r in ranked[:top]:
        display.append({
            "ticker": r["ticker"],
            "bars": r["n_bars"],
            "off%": f"{r['off_frac'] * 100:.0f}",
            "cagr_n%": f"{r['cagr_n'] * 100:.1f}",
            "total_n%": f"{r['total_return_n'] * 100:.0f}",
            "maxdd_n%": f"{r['max_dd_n'] * 100:.1f}",
            "calmar_n": f"{r['calmar_n']:.2f}",
            "sharpe_n": f"{r['sharpe_n']:.2f}",
            "park%": f"{r['park_return'] * 100:.0f}",
            "corr": f"{r['corr_off']:.2f}",
            "cagr_f%": f"{r['cagr_f'] * 100:.1f}",
            "maxdd_f%": f"{r['max_dd_f'] * 100:.1f}",
            "calmar_f": f"{r['calmar_f']:.2f}",
        })
    lines.append(pd.DataFrame(display).to_string(index=False))
    return "\n".join(lines)


def current_recommendation(held: pd.Series, ranked: list) -> str:
    """One-line 'what to do now' read from the most recent held value."""
    date = str(held.index[-1])[:10]
    if bool(held.iloc[-1]):
        return f"[{date}] Current state: IN-MARKET — hold the primary."
    top = ranked[0]["ticker"] if ranked else "(no qualifying backup)"
    return f"[{date}] Current state: FLAT — park in top backup: {top}."
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 src/test_rebound.py`
Expected: `Results: 15 passed, 0 failed out of 15`.

- [ ] **Step 5: Commit**

```bash
git add src/rebound.py src/test_rebound.py
git commit -m "feat(rebound): ranking table + current recommendation"
```

---

### Task 9: Equity/drawdown plot (`plot_curves`) + matplotlib dependency

**Files:**
- Modify: `src/rebound.py`
- Modify: `src/test_rebound.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: `primary_df`, `held`, `primary_ret`, ranked rows, `candidate_frames`, `daily_returns`, `stitch`, `equity_curve`, `max_drawdown`, `baseline_metrics` inputs.
- Produces: `plot_curves(primary_df, held, primary_ret, ranked, candidate_frames, *, mode="naked", top_k=3, out_path, title) -> str` (returns the saved path). matplotlib is imported lazily inside this function and configured with the `Agg` backend.

- [ ] **Step 1: Add the dependency**

Append `matplotlib` to `requirements.txt` (one line, unpinned, matching the file's existing style) and install it:

```bash
printf 'matplotlib\n' >> requirements.txt
python3 -m pip install matplotlib
```

- [ ] **Step 2: Write the failing test**

Add to `src/test_rebound.py` and register `("plot_curves", test_plot_curves)`:

```python
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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 src/test_rebound.py`
Expected: FAIL — `ImportError: cannot import name 'plot_curves'`.

- [ ] **Step 4: Write minimal implementation**

Add to `src/rebound.py`:

```python
def plot_curves(primary_df: pd.DataFrame, held: pd.Series,
                primary_ret: pd.Series, ranked: list, candidate_frames: dict, *,
                mode: str = "naked", top_k: int = 3, out_path: str,
                title: str = "") -> str:
    """Save a two-panel PNG: (top) rebased equity for the two baselines and the
    top-k combined curves, (bottom) the rank-1 combined drawdown. Returns out_path.

    matplotlib is imported lazily with the Agg backend so the module imports
    without a display (and without matplotlib installed for non-plot use)."""
    import os
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    picks = ranked[:top_k]
    # shared window = primary index ∩ every plotted candidate's index
    common = held.index
    for r in picks:
        common = common.intersection(candidate_frames[r["ticker"]].index)

    def rebased(ret: pd.Series) -> pd.Series:
        return equity_curve(ret.reindex(common).fillna(0.0))

    h = held.reindex(common).fillna(False).astype(bool)
    pr = primary_ret.reindex(common).fillna(0.0)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1]})

    ax1.plot(common, rebased(pr).values, label="Primary Buy&Hold",
             color="black", lw=1.2)
    ax1.plot(common, rebased(pr.where(h, 0.0)).values,
             label="Primary + cash", color="grey", lw=1.0, ls="--")

    rank1_dd_equity = None
    for r in picks:
        cr = daily_returns(candidate_frames[r["ticker"]]).reindex(common).fillna(0.0)
        ch = candidate_healthy(candidate_frames[r["ticker"]]).reindex(common).fillna(False).astype(bool)
        s = stitch(h, pr, cr, mode=mode, cand_healthy=ch)
        eq = equity_curve(s["ret"])
        ax1.plot(common, eq.values, lw=1.1, label=f"+ {r['ticker']} ({mode})")
        if rank1_dd_equity is None:
            rank1_dd_equity = eq

    ax1.set_yscale("log")
    ax1.set_ylabel("Equity (log, rebased=1.0)")
    ax1.set_title(title)
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(True, which="both", alpha=0.3)

    if rank1_dd_equity is not None:
        peak = rank1_dd_equity.cummax()
        dd = (rank1_dd_equity / peak - 1.0) * 100
        ax2.fill_between(common, dd.values, 0, color="red", alpha=0.3)
        ax2.set_ylabel("Rank-1 DD %")
    ax2.grid(True, alpha=0.3)

    os.makedirs(os.path.dirname(out_path), exist_ok=True) if os.path.dirname(out_path) else None
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return out_path
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 src/test_rebound.py`
Expected: `Results: 16 passed, 0 failed out of 16`.

- [ ] **Step 6: Commit**

```bash
git add src/rebound.py src/test_rebound.py requirements.txt
git commit -m "feat(rebound): equity/drawdown plot + matplotlib dependency"
```

---

### Task 10: CLI wiring (`scan`, `main`) + documentation

**Files:**
- Modify: `src/rebound.py`
- Modify: `src/test_rebound.py`
- Modify: `README.md`, `README.zh.md`, `CLAUDE.md`

**Interfaces:**
- Consumes: everything above + `data_loader` (`load_ohlc`, `list_universe`).
- Produces: `scan(...) -> (primary_df, held, primary_ret, rows, baselines, frames)`; `main()` (argparse entry point).

- [ ] **Step 1: Write the failing test**

`scan`/`main` do disk + CLI I/O, so the unit test covers only the argument parser wiring via a `build_parser()` helper. Add to `src/test_rebound.py` and register `("arg parser", test_build_parser)`:

```python
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
    assert d.ma == 120 and d.sort == "cagr" and d.plot_mode == "naked"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 src/test_rebound.py`
Expected: FAIL — `ImportError: cannot import name 'build_parser'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/rebound.py`:

```python
def scan(primary_ticker: str, rule: str, params: dict, *,
         candidates: list | None = None, limit: int = 0, cost_bps: float = 0.0,
         health_ma: int = 50, min_overlap: int = 252, min_bars: int = 756):
    """Load the primary + candidate pool from the cache and evaluate all.

    Returns (primary_df, held, primary_ret, rows, baselines, frames)."""
    import data_loader as dl

    primary_df = dl.load_ohlc(primary_ticker)
    held = primary_held(primary_df, rule, **params)
    primary_ret = daily_returns(primary_df)

    if candidates:
        pool = [t for t in candidates if t != primary_ticker]
    else:
        pool = [t for t in dl.list_universe(min_bars=min_bars)
                if t != primary_ticker]
    if limit:
        pool = pool[:limit]

    frames = {}
    for i, ticker in enumerate(pool, 1):
        if i % 200 == 0:
            print(f"  loading {i}/{len(pool)} ...")
        try:
            frames[ticker] = dl.load_ohlc(ticker)
        except Exception:
            continue

    rows = evaluate_all(primary_df, held, primary_ret, frames,
                        cost_bps=cost_bps, health_ma=health_ma,
                        min_overlap=min_overlap)
    baselines = baseline_metrics(held, primary_ret)
    return primary_df, held, primary_ret, rows, baselines, frames


def build_parser():
    import argparse
    p = argparse.ArgumentParser(
        description="Rebound (沸羊羊 / 备胎 finder): rank parking assets for a "
                    "primary stock's flat periods.")
    p.add_argument("ticker", help="Primary stock symbol (must be in the OHLC cache)")
    p.add_argument("--rule", required=True,
                   choices=["price_above_ma", "ma_cross"])
    p.add_argument("--ma", type=int, default=120, help="MA window for price_above_ma")
    p.add_argument("--fast", type=int, default=5, help="Fast MA for ma_cross")
    p.add_argument("--slow", type=int, default=30, help="Slow MA for ma_cross")
    p.add_argument("--max-dd", type=float, default=25.0,
                   help="Drop candidates whose naked max drawdown exceeds this %% ")
    p.add_argument("--sort", default="cagr",
                   choices=["cagr", "total_return", "calmar", "sharpe"])
    p.add_argument("--top", type=int, default=30)
    p.add_argument("--plot-mode", dest="plot_mode", default="naked",
                   choices=["naked", "filtered"])
    p.add_argument("--top-k", dest="top_k", type=int, default=3)
    p.add_argument("--candidates", default=None,
                   help="Comma-separated candidate tickers (else full cache)")
    p.add_argument("--cost-bps", dest="cost_bps", type=float, default=0.0)
    p.add_argument("--out", default=None, help="Output PNG path")
    p.add_argument("--limit", type=int, default=0,
                   help="Restrict candidate pool to first N tickers (smoke test)")
    return p


def main() -> None:
    from datetime import datetime

    args = build_parser().parse_args()
    if args.rule == "price_above_ma":
        params = {"ma": args.ma}
    else:
        params = {"fast": args.fast, "slow": args.slow}

    candidates = ([c.strip() for c in args.candidates.split(",") if c.strip()]
                  if args.candidates else None)

    print(f"Scanning backups for {args.ticker} ({args.rule} {params}) ...")
    primary_df, held, primary_ret, rows, baselines, frames = scan(
        args.ticker, args.rule, params, candidates=candidates,
        limit=args.limit, cost_bps=args.cost_bps)

    ranked = rank_candidates(rows, sort_key=args.sort,
                             max_dd_cap=args.max_dd / 100.0, top=args.top)

    print()
    print("*** Caveat: full-market pool — the rank-1 backup may simply be a name "
          "that trended up over the window. Read alongside corr_off / park_return "
          "/ window length, not in isolation. ***")
    print()
    print(render_table(baselines, ranked, top=args.top))
    print()
    print(current_recommendation(held, ranked))

    out = args.out or (f"data/plots/rebound_{args.ticker}_{args.rule}_"
                       f"{datetime.utcnow().date()}.png")
    if ranked:
        title = f"{args.ticker} {args.rule} {params} — backups ({args.plot_mode})"
        plot_curves(primary_df, held, primary_ret, ranked, frames,
                    mode=args.plot_mode, top_k=args.top_k, out_path=out, title=title)
        print(f"\nSaved curve: {out}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 src/test_rebound.py`
Expected: `Results: 17 passed, 0 failed out of 17`.

- [ ] **Step 5: Documentation updates**

Add a `### 沸羊羊 / Rebound (backup-asset finder)` subsection to `README.md` (English) under the commands/usage area, mirror it in `README.zh.md` (Chinese), and update `CLAUDE.md`. Use this content for `README.md` (translate faithfully for `README.zh.md`):

````markdown
### Rebound (沸羊羊 / backup-asset finder)

Given a primary stock and a hold/flat moving-average rule, rank candidate
"parking" assets to deploy capital into while the primary is flat, and plot the
stitched equity curve.

```bash
# Hold while price > MA120; rank backups for the flat periods
python3 src/rebound.py TSLA --rule price_above_ma --ma 120

# Hold while MA5 > MA30
python3 src/rebound.py TSLA --rule ma_cross --fast 5 --slow 30
```

| Flag | Default | Meaning |
|------|---------|---------|
| `--rule` | (required) | `price_above_ma` or `ma_cross` |
| `--ma` / `--fast` / `--slow` | 120 / 5 / 30 | MA windows |
| `--max-dd` | 25 | Drop candidates whose naked max drawdown exceeds this % |
| `--sort` | `cagr` | `cagr` \| `total_return` \| `calmar` \| `sharpe` (ranks naked mode) |
| `--top` / `--top-k` | 30 / 3 | Table rows / curves overlaid on the plot |
| `--plot-mode` | `naked` | `naked` \| `filtered` (candidate held only when close > MA50) |
| `--candidates` | — | Comma-separated pool (else full cache) |
| `--cost-bps` | 0 | One-way cost per switch day |
| `--out` | `data/plots/...png` | Curve PNG path |
| `--limit` | 0 | First-N candidates (smoke test) |

Outputs a ranking table (naked + filtered metrics, with antifragility reads
`off_frac` / `park_return` / `corr_off`), a "current recommendation" line, and an
equity/drawdown PNG. Requires a populated OHLC cache
(`python3 src/download_ohlc.py --init`). Design: `docs/superpowers/specs/2026-06-25-rebound-design.md`.
````

For `CLAUDE.md`: add a row to the "Project files" table —
`| `src/rebound.py` | Backup-asset finder (沸羊羊): rank parking assets for a primary stock's flat periods |` —
add `matplotlib` to the Dependencies section, and add a short "Rebound" entry under "Available commands".

- [ ] **Step 6: Verify the full suite + run a smoke test if the cache exists**

Run: `python3 src/test_rebound.py`
Expected: `Results: 17 passed, 0 failed out of 17`.

If the cache is populated, smoke-test end to end (otherwise note it is skipped):

```bash
python3 src/rebound.py TSLA --rule ma_cross --fast 5 --slow 30 --limit 30
```
Expected: a baselines block, a ranking table, a recommendation line, and a saved PNG path.

- [ ] **Step 7: Commit**

```bash
git add src/rebound.py src/test_rebound.py README.md README.zh.md CLAUDE.md
git commit -m "feat(rebound): CLI entry point + documentation"
```

---

## Optional: reference-model cross-check (manual, needs populated cache)

Directional validation against the prior-art models (not part of the test suite):

```bash
python3 src/download_ohlc.py --init       # populate the cache first
python3 src/rebound.py TSLA --rule ma_cross --fast 5 --slow 30   # expect AZO / ORLY high
python3 src/rebound.py NVDA --rule price_above_ma --ma 225        # expect TJX / ROST high
```

If AZO/ORLY/TJX/ROST are absent from the cache, note it and skip — exact reproduction
is not expected (the references use 2-name equal-weight baskets + fees + leverage).
