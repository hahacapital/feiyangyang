# feiyangyang Web Service Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wrap the `rebound` backup-finder engine in a personal FastAPI web service — enter a ticker, pick a strategy, set params, run a background full-market scan with live progress, and read the ranked backups plus an interactive ECharts equity/regime/drawdown chart — deployed to AWS ECS Fargate behind the `ff.theblueprint.xyz` ALB.

**Architecture:** One FastAPI process serves a JSON API and a static single-page app. The engine's pure functions run over a warm in-memory `{ticker: DataFrame}` store loaded at startup (S3 sync → disk → RAM), bypassing `scan()`'s per-request disk re-read. Scans run as in-memory background jobs on a single-thread executor, streaming batched progress over SSE. The engine is unchanged except two additive helpers (`evaluate_iter`, `curve_series`).

**Tech Stack:** Python ≥3.10, FastAPI 0.138.1, uvicorn[standard] 0.49.0, sse-starlette 3.4.5, pydantic 2.x, boto3, pandas/numpy/pyarrow (existing), Apache ECharts 6.1.0 (vendored), Docker (python:3.12-slim), ECS Fargate.

## Global Constraints

Every task's requirements implicitly include this section. Values copied verbatim from the design spec (`docs/2026-06-26-webservice-design.md`).

- **Language for committed code/docs/comments:** English. (Chat may be Chinese.)
- **Doc-sync rule:** any source change updates `README.md` (and `CLAUDE.md` where structure changes) in the same change.
- **Dependency pins:** `fastapi==0.138.1`, `uvicorn[standard]==0.49.0`, `sse-starlette==3.4.5`, `boto3` (latest), plus existing `pandas numpy pyarrow matplotlib`. Do **not** pin `starlette` below `0.49.1` (sse-starlette requires it; let pip resolve). Base image `python:3.12-slim`. Pydantic models use `typing.Optional[...]` (not PEP 604 `X | None`) so they import on Python 3.9 too — this worktree's local interpreter is 3.9.x, and `X | None` field annotations crash pydantic v2 at import there.
- **Single worker only.** Run uvicorn with one worker; never set `--workers`/`WEB_CONCURRENCY`. The ~1 GB warm cache and the in-memory job registry must live in one process.
- **Engine is behavior-frozen.** `rebound.py`/`data_loader.py` keep all invariants: no-lookahead (`.shift(1, fill_value=False)`), metrics as fractions, ranking on `_n` keys, default sort `antifragile = cagr_n·(1−corr_off)`. Only **additive** functions may be added to `rebound.py`; `plot_curves` may be refactored only so its output is unchanged (its test must stay green).
- **Engine-reuse contract** (web layer must replicate `scan()`/`main()`): exclude the requested primary from candidates; load the primary on demand (it may be <756 bars / a `=` futures ticker not in the warm set); build the **rule-specific** params dict exactly (`price_above_ma → {ma}`, `ma_cross → {fast, slow}`) and reject foreign keys (`primary_held` silently ignores wrong keys); `max_dd_cap = max_dd/100`, `min_history_bars = int(min_history·252)`, default `min_history = 5.0`; pass `min_overlap=252`, `health_ma=50`; validate `sort ∈ {antifragile,cagr,total_return,calmar,sharpe}`.
- **Import model / run command:** modules under `src/` use bare imports. Run the server with `src/` as the import root: `uvicorn webapp.app:app` launched with working dir `src/` (Docker `WORKDIR /app/src`). `webapp/app.py` also inserts its parent on `sys.path` defensively.
- **Test runner:** hand-rolled, matching `test_rebound.py` (no pytest). `src/test_webapp.py` has a `__main__` block running a list of `(name, fn)` and `sys.exit(1 if failed)`. Run all: `python3 src/test_webapp.py`. Engine tests stay in `src/test_rebound.py`.
- **Palette (UI):** `ink #12161C`, `panel #232B38`, `edge #38424F`, `live #2BB8E6` (in-market), `standby #F2A93B` (parked), `cash #8A93A3`, `drawdown #E8806B`, `paper #ECEFF3`. Hue alone is banned for the two signals — every signal also carries a non-color channel (line style / pattern / text label).
- **Copy (end-user voice, active):** button `RUN SCAN`; states `LIVE — holding TSLA` / `STANDBY — park in GLD` / `CASH`; empty `Pick a ticker and run a scan.`; warming `Warming the cache — N / M loaded`; errors `TSLA isn't in the cache. Try another symbol.` and `Service restarted — please re-run.`
- **ECharts:** version 6.1.0 vendored at `src/webapp/static/vendor/echarts.min.js`. Never put the drawdown series (≤0) on the log equity axis.

---

## File Structure

```
src/rebound.py                     # MODIFY: + evaluate_iter, + curve_series; plot_curves delegates
src/webapp/__init__.py             # CREATE: empty package marker
src/webapp/schemas.py              # CREATE: pydantic ScanRequest + validation + conversions
src/webapp/engine_service.py       # CREATE: warm store, pluggable primary loader, run_scan, dev fixture
src/webapp/jobs.py                 # CREATE: in-memory job registry, single-thread executor, progress, cancel
src/webapp/cache_sync.py           # CREATE: boto3 S3 prefix sync to disk
src/webapp/idle.py                 # CREATE: idle monitor + ECS UpdateService(desiredCount=0)
src/webapp/app.py                  # CREATE: FastAPI lifespan, routes, SSE, static mount, idle middleware
src/webapp/static/index.html       # CREATE: SPA shell
src/webapp/static/styles.css       # CREATE: standby-console theme
src/webapp/static/app.js           # CREATE: fetch/SSE client, table, 3-panel ECharts
src/webapp/static/vendor/echarts.min.js  # CREATE: vendored ECharts 6.1.0
src/test_webapp.py                 # CREATE: hand-rolled web-layer tests
requirements.txt                   # MODIFY: + fastapi, uvicorn[standard], sse-starlette, boto3
Dockerfile                         # CREATE
deploy/task-definition.json        # CREATE
deploy/iam/task-role-policy.json   # CREATE
deploy/iam/trust-policy.json       # CREATE
deploy/deploy.sh                   # CREATE
deploy/wake.sh                     # CREATE
deploy/.env.example                # CREATE
deploy/README.md                   # CREATE
README.md                          # MODIFY: web service section
CLAUDE.md                          # MODIFY: webapp module map + gotchas
```

---

## Task 1: Engine additions — `evaluate_iter` + `curve_series`, `plot_curves` delegates

**Files:**
- Modify: `src/rebound.py` (add `evaluate_iter` near `evaluate_all`; add `curve_series` + `_regime_segments` near `plot_curves`; refactor `plot_curves` body)
- Test: `src/test_rebound.py` (add 4 tests + register them in `__main__`)

**Interfaces:**
- Consumes: existing `daily_returns`, `candidate_healthy`, `stitch`, `equity_curve`, `evaluate_candidate`.
- Produces:
  - `evaluate_iter(primary_df, held, primary_ret, candidate_frames, *, cost_bps=0.0, health_ma=50, min_overlap=252) -> Iterator[tuple[str, dict | None]]`
  - `curve_series(primary_df, held, primary_ret, ranked, candidate_frames, *, mode="naked", top_k=3) -> dict` with keys `dates: list[str]`, `primary_buy_hold: list[float]`, `primary_cash: list[float]`, `picks: list[{ticker:str, mode:str, equity:list[float]}]`, `rank1_drawdown: list[float]` (percent, ≤0, from picks[0] only; `[]` if no picks), `regime: list[{start:str, end:str, state:str}]` (`state ∈ {primary,candidate,cash}`, from picks[0]'s `stitch().active`; `[]` if no picks).

- [ ] **Step 1: Write failing tests** — append to `src/test_rebound.py` (before the `if __name__` block):

```python
def test_evaluate_iter_matches_evaluate_all():
    from rebound import primary_held, daily_returns, evaluate_iter, evaluate_all
    primary = make_df(list(range(1, 31)))
    held = primary_held(primary, "price_above_ma", ma=3)
    p_ret = daily_returns(primary)
    frames = {"A": make_df([100 + i for i in range(30)]),
              "B": make_df([200 - i for i in range(30)]),
              "SHORT": make_df([1, 2, 3])}
    pairs = list(evaluate_iter(primary, held, p_ret, frames, min_overlap=5))
    assert [t for t, _ in pairs] == ["A", "B", "SHORT"]       # yields every ticker, in order
    assert dict(pairs)["SHORT"] is None                        # thin -> None (not dropped)
    rows = [r for _, r in pairs if r is not None]
    assert {r["ticker"] for r in rows} == {r["ticker"] for r in
            evaluate_all(primary, held, p_ret, frames, min_overlap=5)}


def test_curve_series_shape_and_window():
    from rebound import primary_held, daily_returns, curve_series
    primary = make_df(list(range(1, 61)))                      # 60 bars from 2020-01-01
    frames = {"A": make_df([100 + i for i in range(60)]),
              "B": make_df([100 + 2 * i for i in range(40)], start="2020-02-01")}  # shorter, later
    held = primary_held(primary, "price_above_ma", ma=3)
    p_ret = daily_returns(primary)
    ranked = [{"ticker": "A"}, {"ticker": "B"}]
    cs = curve_series(primary, held, p_ret, ranked, frames, mode="naked", top_k=2)
    common = held.index.intersection(frames["A"].index).intersection(frames["B"].index)
    assert len(cs["dates"]) == len(common)                     # shared top-k window
    assert len(cs["primary_buy_hold"]) == len(cs["dates"])
    assert len(cs["primary_cash"]) == len(cs["dates"])
    assert [p["ticker"] for p in cs["picks"]] == ["A", "B"]
    assert all(len(p["equity"]) == len(cs["dates"]) for p in cs["picks"])
    assert len(cs["rank1_drawdown"]) == len(cs["dates"])


def test_curve_series_no_renormalization():
    from rebound import primary_held, daily_returns, curve_series
    # candidate starts a month later, so the shared window's first bar is mid-primary
    # where the primary's return is nonzero -> rebased first point is NOT 1.0.
    primary = make_df(list(range(1, 61)))
    frames = {"A": make_df([100 + i for i in range(40)], start="2020-02-01")}
    held = primary_held(primary, "price_above_ma", ma=3)
    p_ret = daily_returns(primary)
    cs = curve_series(primary, held, p_ret, [{"ticker": "A"}], frames, top_k=1)
    assert abs(cs["primary_buy_hold"][0] - 1.0) > 1e-9, "first point must not be forced to 1.0"
    assert all(d <= 1e-9 for d in cs["rank1_drawdown"]), "drawdown is <= 0 (percent)"


def test_curve_series_regime_tiles_window():
    from rebound import primary_held, daily_returns, curve_series
    primary = make_df([10, 9, 8, 7, 6, 5, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24])
    frames = {"A": make_df([50 + i for i in range(16)])}
    held = primary_held(primary, "ma_cross", fast=2, slow=4)
    p_ret = daily_returns(primary)
    cs = curve_series(primary, held, p_ret, [{"ticker": "A"}], frames, mode="naked", top_k=1)
    seg = cs["regime"]
    assert seg, "expected at least one regime segment"
    assert all(s["state"] in {"primary", "candidate", "cash"} for s in seg)
    assert seg[0]["start"] == cs["dates"][0] and seg[-1]["end"] == cs["dates"][-1]  # tiles fully
```

- [ ] **Step 2: Register the tests** in the `__main__` list in `src/test_rebound.py` (after the `("plot_curves", test_plot_curves),` line):

```python
        ("evaluate_iter", test_evaluate_iter_matches_evaluate_all),
        ("curve_series shape/window", test_curve_series_shape_and_window),
        ("curve_series no-renorm", test_curve_series_no_renormalization),
        ("curve_series regime tiles", test_curve_series_regime_tiles_window),
```

- [ ] **Step 3: Run to verify failure**

Run: `python3 src/test_rebound.py`
Expected: 4 new FAILs (`cannot import name 'evaluate_iter'` / `'curve_series'`); the 18 existing tests still PASS.

- [ ] **Step 4: Add `evaluate_iter` and make `evaluate_all` delegate** — in `src/rebound.py`, replace the body of `evaluate_all` (currently the `rows=[]; for ... try/except` loop) and add `evaluate_iter` just above it:

```python
def evaluate_iter(primary_df: pd.DataFrame, held: pd.Series,
                  primary_ret: pd.Series, candidate_frames: dict, *,
                  cost_bps: float = 0.0, health_ma: int = 50,
                  min_overlap: int = 252):
    """Yield (ticker, row_or_None) for every candidate frame, in dict order.

    Same per-candidate logic as evaluate_all (errors/thin overlaps -> None), but
    streamed so callers can report progress and check for cancellation.
    """
    for ticker, cand_df in candidate_frames.items():
        try:
            row = evaluate_candidate(ticker, primary_df, held, primary_ret,
                                     cand_df, cost_bps=cost_bps,
                                     health_ma=health_ma, min_overlap=min_overlap)
        except Exception:
            row = None
        yield ticker, row


def evaluate_all(primary_df: pd.DataFrame, held: pd.Series,
                 primary_ret: pd.Series, candidate_frames: dict, *,
                 cost_bps: float = 0.0, health_ma: int = 50,
                 min_overlap: int = 252) -> list:
    """Evaluate every candidate frame; skip thin/erroring ones."""
    return [row for _, row in evaluate_iter(
        primary_df, held, primary_ret, candidate_frames, cost_bps=cost_bps,
        health_ma=health_ma, min_overlap=min_overlap) if row is not None]
```

- [ ] **Step 5: Add `curve_series` + `_regime_segments`** — in `src/rebound.py`, add immediately above `def plot_curves(`:

```python
def _regime_segments(active: pd.Series) -> list:
    """Run-length-encode a per-day 'active' Series ('primary'/'candidate'/'cash')
    into contiguous [start, end, state] segments that tile the index with no gaps."""
    if len(active) == 0:
        return []
    grp = (active != active.shift()).cumsum()
    segs = []
    for _, block in active.groupby(grp, sort=False):
        segs.append({"start": block.index[0].strftime("%Y-%m-%d"),
                     "end": block.index[-1].strftime("%Y-%m-%d"),
                     "state": str(block.iloc[0])})
    return segs


def curve_series(primary_df: pd.DataFrame, held: pd.Series,
                 primary_ret: pd.Series, ranked: list, candidate_frames: dict, *,
                 mode: str = "naked", top_k: int = 3) -> dict:
    """JSON-able equivalent of plot_curves' series math (so the two cannot drift).

    Uses plot_curves' SHARED top-k window (held.index ∩ each plotted candidate's
    index), emits equity_curve verbatim with NO re-normalization (the rebased
    first point is genuinely ~1.0, not forced to 1.0), and derives the rank-1
    drawdown + regime segments from the FIRST pick only.
    """
    picks = ranked[:top_k]
    common = held.index
    for r in picks:
        common = common.intersection(candidate_frames[r["ticker"]].index)

    h = held.reindex(common).fillna(False).astype(bool)
    pr = primary_ret.reindex(common).fillna(0.0)
    dates = [d.strftime("%Y-%m-%d") for d in common]

    out = {
        "dates": dates,
        "primary_buy_hold": equity_curve(pr).tolist(),
        "primary_cash": equity_curve(pr.where(h, 0.0)).tolist(),
        "picks": [],
        "rank1_drawdown": [],
        "regime": [],
    }
    for i, r in enumerate(picks):
        t = r["ticker"]
        cr = daily_returns(candidate_frames[t]).reindex(common).fillna(0.0)
        ch = candidate_healthy(candidate_frames[t]).reindex(common).fillna(False).astype(bool)
        s = stitch(h, pr, cr, mode=mode, cand_healthy=ch)
        eq = equity_curve(s["ret"])
        out["picks"].append({"ticker": t, "mode": mode, "equity": eq.tolist()})
        if i == 0:
            peak = eq.cummax()
            out["rank1_drawdown"] = ((eq / peak - 1.0) * 100.0).tolist()
            out["regime"] = _regime_segments(s["active"])
    return out
```

- [ ] **Step 6: Refactor `plot_curves` to delegate** — in `src/rebound.py`, replace the section of `plot_curves` from `picks = ranked[:top_k]` through the rank-1 drawdown `ax2.fill_between(...)` block with the version below (keeps the lazy matplotlib import, figure setup, axis styling, and `savefig` exactly as they are):

```python
    cs = curve_series(primary_df, held, primary_ret, ranked, candidate_frames,
                      mode=mode, top_k=top_k)
    x = pd.to_datetime(cs["dates"])

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1]})

    ax1.plot(x, cs["primary_buy_hold"], label="Primary Buy&Hold", color="black", lw=1.2)
    ax1.plot(x, cs["primary_cash"], label="Primary + cash", color="grey", lw=1.0, ls="--")
    for p in cs["picks"]:
        ax1.plot(x, p["equity"], lw=1.1, label=f"+ {p['ticker']} ({p['mode']})")

    ax1.set_yscale("log")
    ax1.set_ylabel("Equity (log, rebased=1.0)")
    ax1.set_title(title)
    ax1.legend(loc="upper left", fontsize=8)
    ax1.grid(True, which="both", alpha=0.3)

    if cs["rank1_drawdown"]:
        ax2.fill_between(x, cs["rank1_drawdown"], 0, color="red", alpha=0.3)
        ax2.set_ylabel("Rank-1 DD %")
    ax2.grid(True, alpha=0.3)
```

(This replacement **recreates `fig, (ax1, ax2)` itself**. Replace the entire old span from `picks = ranked[:top_k]` through the `ax2.fill_between(...)` block — including the old `plt.subplots(...)` line, the `common = held.index` intersection loop, the `rebased()` helper, the `h`/`pr` locals, the per-pick stitch loop, and the old `rank1_dd_equity` block; they now live in `curve_series` or in the lines above. Keep the later `fig.tight_layout()` / `fig.savefig(...)` lines unchanged.)

- [ ] **Step 7: Run all engine tests**

Run: `python3 src/test_rebound.py`
Expected: `Results: 22 passed, 0 failed out of 22` (18 original incl. `plot_curves`, + 4 new).

- [ ] **Step 8: Commit**

```bash
git add src/rebound.py src/test_rebound.py
git commit -m "feat(engine): add evaluate_iter + curve_series; plot_curves delegates"
```

---

## Task 2: Dependencies, webapp package, request schema + validation

**Files:**
- Modify: `requirements.txt`
- Create: `src/webapp/__init__.py`, `src/webapp/schemas.py`, `src/test_webapp.py`

**Interfaces:**
- Produces: `webapp.schemas.ScanRequest` (pydantic) with fields `ticker, rule, ma, fast, slow, mode, sort, max_dd, min_history, top, top_k, cost_bps` and methods `params() -> dict`, `max_dd_cap() -> float | None`, `min_history_bars() -> int`. Invalid input raises `pydantic.ValidationError`.

- [ ] **Step 1: Add dependencies** — set `requirements.txt` to:

```
pandas
numpy
pyarrow
matplotlib
fastapi==0.138.1
uvicorn[standard]==0.49.0
sse-starlette==3.4.5
boto3
```

- [ ] **Step 2: Install**

Run: `python3 -m pip install -r requirements.txt`
Expected: installs FastAPI 0.138.1, uvicorn 0.49.0, sse-starlette 3.4.5 (pulling starlette ≥0.49.1), boto3. No resolver conflict.

- [ ] **Step 3: Create the package marker** — `src/webapp/__init__.py`:

```python
"""feiyangyang web service package (FastAPI layer over the rebound engine)."""
```

- [ ] **Step 4: Write failing tests** — create `src/test_webapp.py`:

```python
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
```

- [ ] **Step 5: Run to verify failure**

Run: `python3 src/test_webapp.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'webapp.schemas'`.

- [ ] **Step 6: Implement the schema** — `src/webapp/schemas.py`:

```python
"""Request schema + validation for the scan API.

Replicates rebound.main()'s rule-specific param construction and unit
conversions, and rejects foreign param keys because primary_held silently
ignores keys that don't apply to the chosen rule."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, model_validator

RULES = {"price_above_ma", "ma_cross"}
SORTS = {"antifragile", "cagr", "total_return", "calmar", "sharpe"}
MODES = {"naked", "filtered"}


class ScanRequest(BaseModel):
    ticker: str
    rule: str
    ma: Optional[int] = None
    fast: Optional[int] = None
    slow: Optional[int] = None
    mode: str = "naked"
    sort: str = "antifragile"
    max_dd: Optional[float] = None     # percent; -> /100
    min_history: float = 5.0           # years; -> *252 bars
    top: int = 30
    top_k: int = 3
    cost_bps: float = 0.0

    @model_validator(mode="after")
    def _validate(self) -> "ScanRequest":
        if not self.ticker or not self.ticker.strip():
            raise ValueError("ticker is required")
        if self.rule not in RULES:
            raise ValueError(f"rule must be one of {sorted(RULES)}")
        if self.rule == "price_above_ma":
            if self.fast is not None or self.slow is not None:
                raise ValueError("price_above_ma takes only 'ma' (drop fast/slow)")
            if self.ma is None or self.ma < 2:
                raise ValueError("ma must be an integer >= 2")
        else:  # ma_cross
            if self.ma is not None:
                raise ValueError("ma_cross takes only 'fast'/'slow' (drop ma)")
            if self.fast is None or self.slow is None:
                raise ValueError("ma_cross requires both fast and slow")
            if not (2 <= self.fast < self.slow):
                raise ValueError("require 2 <= fast < slow")
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {sorted(MODES)}")
        if self.sort not in SORTS:
            raise ValueError(f"sort must be one of {sorted(SORTS)}")
        if self.top < 1 or self.top_k < 1:
            raise ValueError("top and top_k must be >= 1")
        if self.min_history < 0 or self.cost_bps < 0:
            raise ValueError("min_history and cost_bps must be >= 0")
        return self

    def params(self) -> dict:
        return {"ma": self.ma} if self.rule == "price_above_ma" \
            else {"fast": self.fast, "slow": self.slow}

    def max_dd_cap(self) -> Optional[float]:
        return None if self.max_dd is None else self.max_dd / 100.0

    def min_history_bars(self) -> int:
        return int(self.min_history * 252)
```

- [ ] **Step 7: Run tests**

Run: `python3 src/test_webapp.py`
Expected: `Results: 5 passed, 0 failed out of 5`.

- [ ] **Step 8: Commit**

```bash
git add requirements.txt src/webapp/__init__.py src/webapp/schemas.py src/test_webapp.py
git commit -m "feat(webapp): scan request schema with strict rule-specific validation"
```

---

## Task 3: `engine_service` — warm store, pluggable primary loader, `run_scan`, dev fixture

**Files:**
- Create: `src/webapp/engine_service.py`
- Test: `src/test_webapp.py` (add tests + register)

**Interfaces:**
- Consumes: `ScanRequest`; `rebound.{primary_held,daily_returns,evaluate_iter,baseline_metrics,rank_candidates,antifragile_score,curve_series}`; `data_loader.load_ohlc`.
- Produces:
  - `WARM: dict[str, pd.DataFrame]` (module-level warm store)
  - `warm_load(frames: dict) -> None`, `universe() -> list[str]`, `warm_count() -> int`
  - `set_primary_loader(fn) -> None` (override `data_loader.load_ohlc` in tests)
  - `class PrimaryNotFound(Exception)`, `class ScanCancelled(Exception)`
  - `run_scan(req: ScanRequest, *, progress_cb=None, cancel_event=None) -> dict` returning `{as_of, recommendation:{state,top}, baselines, ranked, curves}`
  - `load_dev_fixture() -> None` (deterministic synthetic universe + primary loader, for local/dev/smoke runs)

- [ ] **Step 1: Write failing tests** — append to `src/test_webapp.py` (before `if __name__`):

```python
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
```

Register in `__main__`:

```python
        ("run_scan excludes primary", test_run_scan_excludes_primary_and_loads_on_demand),
        ("run_scan primary missing", test_run_scan_primary_not_in_cache),
        ("run_scan progress/curves", test_run_scan_progress_and_curves),
        ("run_scan cancellation", test_run_scan_cancellation),
        ("dev fixture", test_dev_fixture_loads),
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 src/test_webapp.py`
Expected: FAIL — `No module named 'webapp.engine_service'`.

- [ ] **Step 3: Implement** — `src/webapp/engine_service.py`:

```python
"""Warm in-memory frame store + scan orchestration over the rebound engine.

Replicates scan()'s contract WITHOUT its per-request disk re-read: the candidate
pool is the warm store minus the requested primary, and the primary is loaded on
demand (it may be <756 bars or a '=' futures ticker not in the warm universe)."""
from __future__ import annotations

import os
import sys

import pandas as pd

# Resolve sibling engine modules (src/ is the import root; be defensive).
_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import data_loader as _dl  # noqa: E402
from rebound import (  # noqa: E402
    antifragile_score, baseline_metrics, curve_series, daily_returns,
    evaluate_iter, primary_held, rank_candidates,
)

WARM: dict[str, pd.DataFrame] = {}
_load_primary = _dl.load_ohlc          # overridable in tests / dev
_PROGRESS_EVERY = 50                    # batch SSE progress every N candidates


class PrimaryNotFound(Exception):
    """The requested primary ticker is not in the OHLC cache."""


class ScanCancelled(Exception):
    """The scan was cancelled via its cancel_event."""


def warm_load(frames: dict) -> None:
    WARM.clear()
    WARM.update(frames)


def universe() -> list[str]:
    return sorted(WARM.keys())


def warm_count() -> int:
    return len(WARM)


def set_primary_loader(fn) -> None:
    global _load_primary
    _load_primary = fn


def run_scan(req, *, progress_cb=None, cancel_event=None) -> dict:
    try:
        primary_df = _load_primary(req.ticker)
    except FileNotFoundError as exc:
        raise PrimaryNotFound(req.ticker) from exc

    held = primary_held(primary_df, req.rule, **req.params())
    primary_ret = daily_returns(primary_df)
    candidate_frames = {t: f for t, f in WARM.items() if t != req.ticker}
    total = len(candidate_frames)

    rows = []
    for i, (_ticker, row) in enumerate(evaluate_iter(
            primary_df, held, primary_ret, candidate_frames,
            cost_bps=req.cost_bps, health_ma=50, min_overlap=252), start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise ScanCancelled()
        if row is not None:
            rows.append(row)
        if progress_cb and (i % _PROGRESS_EVERY == 0 or i == total):
            progress_cb(i, total)
    if progress_cb and total == 0:
        progress_cb(0, 0)

    baselines = baseline_metrics(held, primary_ret)
    ranked = rank_candidates(rows, sort_key=req.sort, max_dd_cap=req.max_dd_cap(),
                             top=req.top, min_history_bars=req.min_history_bars())
    ranked_out = [{**r, "afscore": antifragile_score(r)} for r in ranked]

    as_of = str(held.index[-1])[:10]
    state = "in-market" if bool(held.iloc[-1]) else "flat"
    curves = curve_series(primary_df, held, primary_ret, ranked, candidate_frames,
                          mode=req.mode, top_k=req.top_k)
    return {
        "as_of": as_of,
        "recommendation": {"state": state,
                           "top": ranked_out[0]["ticker"] if ranked_out else None},
        "baselines": baselines,
        "ranked": ranked_out,
        "curves": curves,
    }


def load_dev_fixture() -> None:
    """Deterministic synthetic universe + primary for offline dev / smoke tests.

    No S3, no disk. Primary 'DEMO' plus a handful of candidates with distinct
    shapes so the chart, ribbon, and table all have content."""
    import numpy as np

    def synth(seed, n=1500, drift=0.0003, vol=0.012, start="2018-01-01"):
        rng = np.random.default_rng(seed)
        rets = rng.normal(drift, vol, n)
        close = 100.0 * np.cumprod(1.0 + rets)
        idx = pd.bdate_range(start, periods=n)
        return pd.DataFrame({"open": close, "high": close, "low": close,
                             "close": close}, index=idx)

    demo = synth(1, drift=0.0006)
    frames = {"SPY": synth(2, drift=0.0004), "GLD": synth(3, drift=0.0001, vol=0.009),
              "TLT": synth(4, drift=0.00005, vol=0.008), "QQQ": synth(5, drift=0.0007)}
    warm_load(frames)
    set_primary_loader(lambda t: demo if t.upper() == "DEMO"
                       else WARM.get(t.upper()) if t.upper() in WARM
                       else (_ for _ in ()).throw(PrimaryNotFound(t)))
```

- [ ] **Step 4: Run tests**

Run: `python3 src/test_webapp.py`
Expected: `Results: 10 passed, 0 failed out of 10`.

- [ ] **Step 5: Commit**

```bash
git add src/webapp/engine_service.py src/test_webapp.py
git commit -m "feat(webapp): warm-store engine_service with scan + dev fixture"
```

---

## Task 4: `jobs` — background job registry, progress, cancellation

**Files:**
- Create: `src/webapp/jobs.py`
- Test: `src/test_webapp.py` (add tests + register)

**Interfaces:**
- Consumes: `engine_service.run_scan`, `ScanRequest`.
- Produces:
  - `class JobState` with attrs `status` (`queued|running|done|error|cancelled`), `done:int`, `total:int`, `result:dict|None`, `error:str|None`.
  - `submit(req: ScanRequest, server_epoch: str) -> str` (job_id, prefixed with `server_epoch`)
  - `get(job_id: str) -> JobState | None`, `cancel(job_id: str) -> bool`
  - `belongs_to_epoch(job_id: str, server_epoch: str) -> bool`

- [ ] **Step 1: Write failing tests** — append to `src/test_webapp.py`:

```python
def test_jobs_lifecycle():
    import time
    import webapp.engine_service as es
    import webapp.jobs as jobs
    _fixture()
    jid = jobs.submit(ScanRequest(ticker="TSLA", rule="price_above_ma", ma=20, top_k=2), "ep1")
    assert jid.startswith("ep1-")
    for _ in range(200):
        if jobs.get(jid).status in {"done", "error"}:
            break
        time.sleep(0.02)
    job = jobs.get(jid)
    assert job.status == "done", f"got {job.status}: {job.error}"
    assert job.total >= 1 and job.done == job.total
    assert job.result["curves"]["dates"]


def test_jobs_unknown_and_epoch():
    import webapp.jobs as jobs
    assert jobs.get("nope") is None
    assert jobs.belongs_to_epoch("ep1-abcd1234", "ep1") is True
    assert jobs.belongs_to_epoch("ep1-abcd1234", "ep2") is False


def test_jobs_error_surfaces():
    import time
    import webapp.jobs as jobs
    _fixture()
    jid = jobs.submit(ScanRequest(ticker="NOPE", rule="price_above_ma", ma=20), "ep1")
    for _ in range(200):
        if jobs.get(jid).status in {"done", "error"}:
            break
        time.sleep(0.02)
    job = jobs.get(jid)
    assert job.status == "error" and "NOPE" in (job.error or "")
```

Register in `__main__`:

```python
        ("jobs lifecycle", test_jobs_lifecycle),
        ("jobs unknown/epoch", test_jobs_unknown_and_epoch),
        ("jobs error surfaces", test_jobs_error_surfaces),
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 src/test_webapp.py`
Expected: FAIL — `No module named 'webapp.jobs'`.

- [ ] **Step 3: Implement** — `src/webapp/jobs.py`:

```python
"""In-memory background-job registry for scans.

Single-thread executor (one scan at a time) so the warm cache and CPU are not
oversubscribed. Job ids are prefixed with the server epoch so a client can detect
a process restart (the new process won't recognise an old id)."""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from webapp import engine_service


class JobState:
    def __init__(self) -> None:
        self.status = "queued"          # queued|running|done|error|cancelled
        self.done = 0
        self.total = 0
        self.result: dict | None = None
        self.error: str | None = None
        self.cancel_event = threading.Event()


_jobs: dict[str, JobState] = {}
_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scan")


def submit(req, server_epoch: str) -> str:
    job_id = f"{server_epoch}-{uuid.uuid4().hex[:8]}"
    job = JobState()
    with _lock:
        _jobs[job_id] = job
    _executor.submit(_run, job_id, req)
    return job_id


def _run(job_id: str, req) -> None:
    job = _jobs[job_id]
    job.status = "running"

    def progress(done: int, total: int) -> None:
        job.done, job.total = done, total

    try:
        job.result = engine_service.run_scan(
            req, progress_cb=progress, cancel_event=job.cancel_event)
        job.status = "done"
    except engine_service.ScanCancelled:
        job.status = "cancelled"
    except engine_service.PrimaryNotFound as exc:
        job.status = "error"
        job.error = f"{exc} isn't in the cache. Try another symbol."
    except Exception as exc:  # noqa: BLE001 - surface any engine error to the client
        job.status = "error"
        job.error = str(exc)


def get(job_id: str) -> JobState | None:
    return _jobs.get(job_id)


def cancel(job_id: str) -> bool:
    job = _jobs.get(job_id)
    if job is None:
        return False
    job.cancel_event.set()
    return True


def belongs_to_epoch(job_id: str, server_epoch: str) -> bool:
    return job_id.startswith(f"{server_epoch}-")
```

- [ ] **Step 4: Run tests**

Run: `python3 src/test_webapp.py`
Expected: `Results: 13 passed, 0 failed out of 13`.

- [ ] **Step 5: Commit**

```bash
git add src/webapp/jobs.py src/test_webapp.py
git commit -m "feat(webapp): in-memory scan job registry with progress + cancel"
```

---

## Task 5: `app` — FastAPI lifespan, endpoints, SSE, static mount

**Files:**
- Create: `src/webapp/app.py`
- Test: `src/test_webapp.py` (add TestClient tests + register)

**Interfaces:**
- Consumes: `engine_service`, `jobs`, `ScanRequest`, `cache_sync` (Task 6, imported lazily inside lifespan so this task is testable first), `idle` (Task 7, optional).
- Produces: `app` (FastAPI), `STATE` (module dict: `ready:bool`, `loaded:int`, `total:int`, `cache_date:str|None`, `server_epoch:str`). Endpoints per spec §5.3.
- Env flags: `FEIYANG_DEV_FIXTURE=1` → load synthetic fixture, ready immediately; `FEIYANG_SKIP_WARMUP=1` → no S3/disk (tests inject WARM); else prod warmup thread.

- [ ] **Step 1: Write failing tests** — append to `src/test_webapp.py`:

```python
def _client(dev_fixture=True):
    import os
    os.environ["FEIYANG_SKIP_WARMUP"] = "1"
    if dev_fixture:
        import webapp.engine_service as es
        es.load_dev_fixture()
    from fastapi.testclient import TestClient
    from webapp.app import app, STATE
    STATE["ready"] = True
    return TestClient(app)


def test_http_status_and_universe():
    with _client() as c:
        assert c.get("/healthz").status_code == 200
        s = c.get("/api/status").json()
        assert s["state"] == "ready" and "server_epoch" in s
        u = c.get("/api/universe").json()
        assert "SPY" in u["tickers"]


def test_http_scan_validation_422():
    with _client() as c:
        r = c.post("/api/scan", json={"ticker": "DEMO", "rule": "ma_cross",
                                      "fast": 30, "slow": 5})
        assert r.status_code == 422


def test_http_scan_poll_result():
    import time
    with _client() as c:
        r = c.post("/api/scan", json={"ticker": "DEMO", "rule": "ma_cross",
                                      "fast": 5, "slow": 20, "top_k": 2})
        assert r.status_code == 200
        jid = r.json()["job_id"]
        out = None
        for _ in range(200):
            rr = c.get(f"/api/scan/{jid}/result")
            if rr.status_code == 200 and rr.json().get("status") == "done":
                out = rr.json()["result"]; break
            time.sleep(0.02)
        assert out and out["curves"]["dates"]


def test_http_unknown_job_410():
    with _client() as c:
        r = c.get("/api/scan/ZZZ-deadbeef/result")
        assert r.status_code == 410
        assert r.json()["status"] == "unknown_job"
```

Register in `__main__`:

```python
        ("http status/universe", test_http_status_and_universe),
        ("http scan 422", test_http_scan_validation_422),
        ("http scan poll result", test_http_scan_poll_result),
        ("http unknown job 410", test_http_unknown_job_410),
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 src/test_webapp.py`
Expected: FAIL — `No module named 'webapp.app'`.

- [ ] **Step 3: Implement** — `src/webapp/app.py`:

```python
"""FastAPI app: lifespan warmup, scan API with SSE progress, static SPA.

Single worker only (the warm cache + job registry are process-local). Warmup runs
on a background thread so /api/status stays responsive during the 1-3 min cold
start; /healthz is a cheap liveness for the ALB while warming."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse
from starlette.middleware.gzip import GZipMiddleware

_HERE = Path(__file__).resolve().parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from webapp import engine_service, jobs  # noqa: E402
from webapp.schemas import ScanRequest    # noqa: E402

STATE = {
    "ready": False,
    "loaded": 0,
    "total": 0,
    "cache_date": None,
    "server_epoch": uuid.uuid4().hex[:8],
}


def _warmup_prod() -> None:
    """S3 sync -> disk -> warm store. Runs on a background thread."""
    from webapp import cache_sync
    import data_loader as dl
    try:
        cache_sync.sync_cache()
        tickers = dl.list_universe(min_bars=756)
        frames = {}
        for i, t in enumerate(tickers, 1):
            try:
                frames[t] = dl.load_ohlc(t)
            except Exception:
                continue
            STATE["loaded"] = i
            STATE["total"] = len(tickers)
        engine_service.warm_load(frames)
        manifest = dl.read_manifest()
        STATE["cache_date"] = manifest.get("last_update")
    finally:
        STATE["ready"] = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    if os.environ.get("FEIYANG_DEV_FIXTURE") == "1":
        engine_service.load_dev_fixture()
        STATE.update(ready=True, loaded=engine_service.warm_count(),
                     total=engine_service.warm_count(), cache_date="dev-fixture")
    elif os.environ.get("FEIYANG_SKIP_WARMUP") == "1":
        pass  # tests inject WARM and set STATE["ready"]
    else:
        threading.Thread(target=_warmup_prod, name="warmup", daemon=True).start()
    yield


app = FastAPI(title="feiyangyang · STANDBY CONSOLE", lifespan=lifespan)
# Compress JSON responses (the ~49 KB curve payload from /api/scan/{id}/result).
# Tiny SSE progress events are below minimum_size, so the live stream is unaffected.
app.add_middleware(GZipMiddleware, minimum_size=1024)


@app.get("/healthz")
def healthz() -> Response:
    return Response(content="ok", media_type="text/plain")


@app.get("/api/status")
def status() -> dict:
    return {"state": "ready" if STATE["ready"] else "warming",
            "loaded": STATE["loaded"], "total": STATE["total"],
            "cache_date": STATE["cache_date"], "server_epoch": STATE["server_epoch"]}


@app.get("/api/universe")
def universe() -> dict:
    return {"tickers": engine_service.universe()}


@app.post("/api/scan")
def scan(req: ScanRequest) -> dict:
    if not STATE["ready"]:
        return JSONResponse(status_code=503, content={"error": "Warming the cache — try again shortly."})
    job_id = jobs.submit(req, STATE["server_epoch"])
    return {"job_id": job_id, "server_epoch": STATE["server_epoch"]}


@app.get("/api/scan/{job_id}/result")
def scan_result(job_id: str):
    job = jobs.get(job_id)
    if job is None or not jobs.belongs_to_epoch(job_id, STATE["server_epoch"]):
        return JSONResponse(status_code=410,
                            content={"status": "unknown_job",
                                     "server_epoch": STATE["server_epoch"]})
    return {"status": job.status, "done": job.done, "total": job.total,
            "error": job.error, "result": job.result}


@app.get("/api/scan/{job_id}/events")
async def scan_events(job_id: str):
    async def gen():
        job = jobs.get(job_id)
        if job is None or not jobs.belongs_to_epoch(job_id, STATE["server_epoch"]):
            yield {"event": "error", "data": json.dumps(
                {"status": "unknown_job", "server_epoch": STATE["server_epoch"]})}
            return
        last = -1
        while True:
            if job.done != last:
                last = job.done
                yield {"event": "progress",
                       "data": json.dumps({"done": job.done, "total": job.total})}
            if job.status in {"done", "error", "cancelled"}:
                # Signal completion only; the SPA fetches the (gzip-eligible) full
                # result from GET /api/scan/{id}/result so the ~49 KB curve payload
                # is compressed and the SSE stream stays small.
                yield {"event": "result",
                       "data": json.dumps({"status": job.status, "error": job.error})}
                return
            await asyncio.sleep(0.5)
    return EventSourceResponse(gen())


# Static SPA — mounted LAST so /api/* wins. html=True serves index.html at "/".
_STATIC = _HERE / "static"
if _STATIC.is_dir():
    app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")
```

- [ ] **Step 4: Run tests**

Run: `python3 src/test_webapp.py`
Expected: `Results: 17 passed, 0 failed out of 17`.

- [ ] **Step 5: Smoke the dev server manually**

Run: `cd src && FEIYANG_DEV_FIXTURE=1 python3 -m uvicorn webapp.app:app --port 8000` then in another shell `curl -s localhost:8000/api/status` → `{"state":"ready",...}`. Stop the server.

- [ ] **Step 6: Commit**

```bash
git add src/webapp/app.py src/test_webapp.py
git commit -m "feat(webapp): FastAPI app with lifespan warmup, scan API, SSE"
```

---

## Task 6: `cache_sync` — boto3 S3 prefix sync to disk

**Files:**
- Create: `src/webapp/cache_sync.py`
- Test: `src/test_webapp.py` (add tests + register)

**Interfaces:**
- Produces:
  - `local_path_for(prefix: str, key: str, dest_dir: str) -> str` (S3 key → local path under dest, preserving the sub-path after `prefix`)
  - `sync_cache(*, bucket=BUCKET, prefix=PREFIX, dest_dir=None, client=None) -> int` (returns files downloaded; uses `data_loader.DATA_DIR` when `dest_dir` is None)
  - module constants `BUCKET="staking-ledger-bpt"`, `PREFIX="jojo_quant/ohlc/"`

- [ ] **Step 1: Write failing tests** — append to `src/test_webapp.py`:

```python
def test_local_path_for():
    from webapp.cache_sync import local_path_for
    p = local_path_for("jojo_quant/ohlc/", "jojo_quant/ohlc/stocks/AAPL.parquet", "/tmp/ohlc")
    assert p == "/tmp/ohlc/stocks/AAPL.parquet"


def test_sync_cache_with_fake_client(tmp="/tmp/feiyang_synctest"):
    import os, shutil
    shutil.rmtree(tmp, ignore_errors=True)
    from webapp import cache_sync

    class FakeClient:
        def get_paginator(self, _name):
            class P:
                def paginate(self, **_kw):
                    yield {"Contents": [
                        {"Key": "jojo_quant/ohlc/stocks/AAPL.parquet", "Size": 3},
                        {"Key": "jojo_quant/ohlc/_meta.parquet", "Size": 5},
                        {"Key": "jojo_quant/ohlc/", "Size": 0},          # the prefix "folder" -> skipped
                    ]}
            return P()

        def download_file(self, Bucket, Key, Filename):
            os.makedirs(os.path.dirname(Filename), exist_ok=True)
            with open(Filename, "w") as f:
                f.write("x")

    n = cache_sync.sync_cache(dest_dir=tmp, client=FakeClient())
    assert n == 2
    assert os.path.exists(f"{tmp}/stocks/AAPL.parquet")
    assert os.path.exists(f"{tmp}/_meta.parquet")
    shutil.rmtree(tmp, ignore_errors=True)
```

Register in `__main__`:

```python
        ("cache local_path_for", test_local_path_for),
        ("cache sync fake client", test_sync_cache_with_fake_client),
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 src/test_webapp.py`
Expected: FAIL — `No module named 'webapp.cache_sync'`.

- [ ] **Step 3: Implement** — `src/webapp/cache_sync.py`:

```python
"""Sync the OHLC cache from S3 into the local data_loader directory using boto3.

boto3 reads the ECS task-role credentials via the container credential provider —
no AWS CLI needed in the image. Mirrors scripts/pull_cache.sh's bucket/prefix."""
from __future__ import annotations

import os

BUCKET = "staking-ledger-bpt"
PREFIX = "jojo_quant/ohlc/"


def local_path_for(prefix: str, key: str, dest_dir: str) -> str:
    rel = key[len(prefix):] if key.startswith(prefix) else key
    return os.path.join(dest_dir, rel)


def sync_cache(*, bucket: str = BUCKET, prefix: str = PREFIX,
               dest_dir: str | None = None, client=None) -> int:
    """Download every object under s3://bucket/prefix into dest_dir. Skips the
    prefix 'folder' marker and objects already present with the same size.
    Returns the number of files written."""
    if dest_dir is None:
        import data_loader as dl
        dest_dir = str(dl.DATA_DIR)
    if client is None:
        import boto3
        client = boto3.client("s3")

    written = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key, size = obj["Key"], obj.get("Size", 0)
            if key.endswith("/") or (key == prefix):
                continue
            dest = local_path_for(prefix, key, dest_dir)
            if os.path.exists(dest) and os.path.getsize(dest) == size:
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            client.download_file(Bucket=bucket, Key=key, Filename=dest)
            written += 1
    return written
```

- [ ] **Step 4: Run tests**

Run: `python3 src/test_webapp.py`
Expected: `Results: 19 passed, 0 failed out of 19`.

- [ ] **Step 5: Commit**

```bash
git add src/webapp/cache_sync.py src/test_webapp.py
git commit -m "feat(webapp): boto3 S3 cache sync into the data_loader directory"
```

---

## Task 7: `idle` — idle auto-stop monitor (ECS desiredCount=0)

**Files:**
- Create: `src/webapp/idle.py`
- Modify: `src/webapp/app.py` (activity middleware + `/api/idle-policy` endpoints + start monitor in lifespan)
- Test: `src/test_webapp.py` (add tests + register)

**Interfaces:**
- Produces:
  - `class IdleMonitor(ecs_client, cluster, service, *, now=time.time)` with `touch()`, `set_minutes(m: float | None)`, `get_minutes()`, `should_stop(now=None) -> bool`, `maybe_stop() -> bool` (calls `ecs.update_service(... desiredCount=0)` when idle past the window; returns True if it stopped).
- App: `GET /api/idle-policy` → `{minutes}`; `POST /api/idle-policy {minutes|null}`; an HTTP middleware calls `monitor.touch()` on every `/api/*` request; a daemon thread calls `maybe_stop()` once a minute (prod only).

- [ ] **Step 1: Write failing tests** — append to `src/test_webapp.py`:

```python
def test_idle_monitor_logic():
    from webapp.idle import IdleMonitor
    clock = {"t": 1000.0}

    class FakeEcs:
        def __init__(self): self.calls = []
        def update_service(self, **kw): self.calls.append(kw)

    ecs = FakeEcs()
    m = IdleMonitor(ecs, "ff", "feiyang", now=lambda: clock["t"])
    assert m.should_stop() is False                 # disabled by default
    m.set_minutes(5)
    m.touch()
    clock["t"] += 4 * 60
    assert m.should_stop() is False                 # 4 < 5 min idle
    clock["t"] += 2 * 60
    assert m.should_stop() is True                  # 6 >= 5 min idle
    assert m.maybe_stop() is True
    assert ecs.calls[0]["desiredCount"] == 0
    assert ecs.calls[0]["cluster"] == "ff" and ecs.calls[0]["service"] == "feiyang"


def test_idle_policy_endpoints():
    with _client() as c:
        assert c.get("/api/idle-policy").json()["minutes"] is None
        c.post("/api/idle-policy", json={"minutes": 30})
        assert c.get("/api/idle-policy").json()["minutes"] == 30
```

Register in `__main__`:

```python
        ("idle monitor logic", test_idle_monitor_logic),
        ("idle policy endpoints", test_idle_policy_endpoints),
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 src/test_webapp.py`
Expected: FAIL — `No module named 'webapp.idle'`.

- [ ] **Step 3: Implement** — `src/webapp/idle.py`:

```python
"""Idle auto-stop: after N idle minutes, set the ECS service desiredCount=0.

The app can stop itself (it is up when the timer fires) but cannot wake itself
(once at 0 there is no task to serve the wake) — wake is out-of-band via
deploy/wake.sh. Default: disabled (minutes=None)."""
from __future__ import annotations

import threading
import time


class IdleMonitor:
    def __init__(self, ecs_client, cluster: str, service: str, *, now=time.time):
        self._ecs = ecs_client
        self._cluster = cluster
        self._service = service
        self._now = now
        self._minutes: float | None = None
        self._last = now()
        self._lock = threading.Lock()

    def touch(self) -> None:
        with self._lock:
            self._last = self._now()

    def set_minutes(self, minutes: float | None) -> None:
        with self._lock:
            self._minutes = minutes
            self._last = self._now()

    def get_minutes(self) -> float | None:
        return self._minutes

    def should_stop(self, now: float | None = None) -> bool:
        if self._minutes is None:
            return False
        now = self._now() if now is None else now
        return (now - self._last) >= self._minutes * 60.0

    def maybe_stop(self) -> bool:
        if not self.should_stop():
            return False
        self._ecs.update_service(cluster=self._cluster, service=self._service,
                                 desiredCount=0)
        return True
```

- [ ] **Step 4: Wire into `app.py`** — add to `src/webapp/app.py`:

  After the imports, add `import time` and a pydantic model + monitor holder near `STATE`:

```python
from pydantic import BaseModel  # add to the existing imports


class IdlePolicy(BaseModel):
    minutes: float | None = None


_MONITOR = {"obj": None}  # set in lifespan (prod) or lazily for tests
```

  In `lifespan`, in the `else:` (prod) branch, after starting the warmup thread, add:

```python
        import boto3
        from webapp.idle import IdleMonitor
        cluster = os.environ.get("ECS_CLUSTER", "ff")
        service = os.environ.get("ECS_SERVICE", "feiyangyang")
        _MONITOR["obj"] = IdleMonitor(boto3.client("ecs"), cluster, service)

        def _idle_loop():
            while True:
                time.sleep(60)
                try:
                    if _MONITOR["obj"] and _MONITOR["obj"].maybe_stop():
                        return
                except Exception:
                    pass
        threading.Thread(target=_idle_loop, name="idle", daemon=True).start()
```

  Add an activity middleware (after `app = FastAPI(...)`):

```python
@app.middleware("http")
async def _touch_idle(request: Request, call_next):
    if _MONITOR["obj"] is not None and request.url.path.startswith("/api/"):
        _MONITOR["obj"].touch()
    return await call_next(request)
```

  Add the idle-policy endpoints (before the static mount). Tests run without a prod monitor, so lazily create a no-op-capable monitor holder when absent:

```python
@app.get("/api/idle-policy")
def get_idle_policy() -> dict:
    m = _MONITOR["obj"]
    return {"minutes": m.get_minutes() if m else _IDLE_FALLBACK["minutes"]}


@app.post("/api/idle-policy")
def set_idle_policy(policy: IdlePolicy) -> dict:
    m = _MONITOR["obj"]
    if m:
        m.set_minutes(policy.minutes)
    else:
        _IDLE_FALLBACK["minutes"] = policy.minutes  # no ECS client (dev/test)
    return {"minutes": policy.minutes}
```

  And declare the fallback near `STATE`:

```python
_IDLE_FALLBACK = {"minutes": None}
```

- [ ] **Step 5: Run tests**

Run: `python3 src/test_webapp.py`
Expected: `Results: 21 passed, 0 failed out of 21`.

- [ ] **Step 6: Commit**

```bash
git add src/webapp/idle.py src/webapp/app.py src/test_webapp.py
git commit -m "feat(webapp): opt-in idle auto-stop (ECS desiredCount=0) + policy API"
```

---

## Task 8: Frontend shell — index.html, styles.css (standby-console theme)

**Files:**
- Create: `src/webapp/static/index.html`, `src/webapp/static/styles.css`

**Interfaces:**
- Produces the DOM the JS (Task 9) drives: `#ticker`, `#rule`, `#ma-field`, `#cross-fields`, `#ma`, `#fast`, `#slow`, `#mode`, `#sort`, `#run`, `#state-plate`, `#progress`, `#table`, `#chart`, `#cache-lamp`. No JS logic yet (a stub `app.js` is added in Task 9).

- [ ] **Step 1: Create `src/webapp/static/index.html`:**

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>沸羊羊 · STANDBY CONSOLE</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link href="https://fonts.googleapis.com/css2?family=Archivo:wght@400;600&family=Archivo+Expanded:wght@600;700&family=Spline+Sans+Mono:wght@400;500&display=swap" rel="stylesheet" />
  <link rel="stylesheet" href="/styles.css" />
</head>
<body>
  <header class="topbar">
    <h1 class="brand">沸羊羊 · <span>STANDBY CONSOLE</span></h1>
    <div class="cache" id="cache-lamp"><span class="lamp warming"></span><span id="cache-text">connecting…</span></div>
  </header>

  <main class="grid">
    <section class="console panel" aria-label="Scan controls">
      <h2 class="eyebrow">CONSOLE</h2>
      <label class="field"><span>Primary ticker</span>
        <input id="ticker" autocomplete="off" placeholder="TSLA" /></label>
      <label class="field"><span>Strategy</span>
        <select id="rule">
          <option value="ma_cross">MA cross (fast / slow)</option>
          <option value="price_above_ma">Price above MA</option>
        </select></label>
      <div id="cross-fields" class="dual">
        <label class="field"><span>Fast</span><input id="fast" type="number" value="5" min="2" /></label>
        <label class="field"><span>Slow</span><input id="slow" type="number" value="30" min="3" /></label>
      </div>
      <label id="ma-field" class="field hidden"><span>MA window</span>
        <input id="ma" type="number" value="120" min="2" /></label>
      <div class="dual">
        <label class="field"><span>Mode</span>
          <select id="mode"><option value="naked">naked</option><option value="filtered">filtered</option></select></label>
        <label class="field"><span>Sort</span>
          <select id="sort">
            <option value="antifragile">antifragile</option>
            <option value="cagr">cagr</option><option value="calmar">calmar</option>
            <option value="sharpe">sharpe</option><option value="total_return">total return</option>
          </select></label>
      </div>
      <button id="run" class="run">RUN SCAN</button>
      <div id="progress" class="progress hidden"><div class="bar"><i></i></div><span class="readout"></span></div>
    </section>

    <section class="verdict panel" aria-label="Recommendation">
      <h2 class="eyebrow">STATE PLATE</h2>
      <div id="state-plate" class="plate idle"><span class="lamp"></span><span class="plate-text">Pick a ticker and run a scan.</span></div>
      <div id="gauges" class="gauges"></div>
    </section>

    <section class="chartwrap panel" aria-label="Equity, regime and drawdown">
      <div class="chart-head"><h2 class="eyebrow">EQUITY · REGIME · DRAWDOWN</h2>
        <div class="legend" id="legend"></div></div>
      <div id="chart" class="chart"></div>
    </section>

    <section class="backups panel" aria-label="Ranked backups">
      <h2 class="eyebrow">BACKUPS</h2>
      <div id="table" class="table"><p class="muted">No scan yet.</p></div>
    </section>
  </main>

  <script src="/vendor/echarts.min.js"></script>
  <script src="/app.js"></script>
</body>
</html>
```

- [ ] **Step 2: Create `src/webapp/static/styles.css`:**

```css
:root{
  --ink:#12161C; --panel:#232B38; --edge:#38424F;
  --live:#2BB8E6; --standby:#F2A93B; --cash:#8A93A3;
  --drawdown:#E8806B; --paper:#ECEFF3;
  --mono:"Spline Sans Mono",ui-monospace,monospace;
  --disp:"Archivo Expanded","Archivo",system-ui,sans-serif;
  --body:"Archivo",system-ui,sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:
   radial-gradient(120% 80% at 50% -10%,#19202b 0%,var(--ink) 60%);
   color:var(--paper);font-family:var(--body);
   /* faint phosphor scanline texture — the instrument-panel risk */
   background-image:repeating-linear-gradient(0deg,rgba(255,255,255,.014) 0 1px,transparent 1px 3px);}
.topbar{display:flex;justify-content:space-between;align-items:center;
   padding:14px 22px;border-bottom:1px solid var(--edge)}
.brand{font-family:var(--disp);font-size:18px;font-weight:700;letter-spacing:.04em;margin:0}
.brand span{color:var(--live)}
.cache{display:flex;gap:8px;align-items:center;font-family:var(--mono);font-size:12px;color:var(--cash)}
.lamp{width:9px;height:9px;border-radius:50%;background:var(--cash);box-shadow:0 0 8px currentColor}
.lamp.warming{background:var(--standby);color:var(--standby);animation:pulse 1.2s infinite}
.lamp.ready{background:var(--live);color:var(--live)}
@keyframes pulse{50%{opacity:.35}}
.grid{display:grid;gap:14px;padding:16px 22px;
   grid-template-columns:300px 1fr;grid-template-areas:"console verdict" "console chart" "backups backups"}
.console{grid-area:console}.verdict{grid-area:verdict}.chartwrap{grid-area:chart}.backups{grid-area:backups}
.panel{background:linear-gradient(180deg,#262f3d,var(--panel));border:1px solid var(--edge);
   border-radius:10px;padding:16px}
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.22em;color:var(--cash);
   text-transform:uppercase;margin:0 0 12px;border-bottom:1px solid var(--edge);padding-bottom:8px}
.field{display:flex;flex-direction:column;gap:5px;margin-bottom:11px;font-size:12px;color:var(--cash)}
.field input,.field select{background:#161c25;border:1px solid var(--edge);color:var(--paper);
   font-family:var(--mono);font-size:14px;padding:9px 10px;border-radius:7px}
.field input:focus,.field select:focus{outline:2px solid var(--live);outline-offset:1px}
.dual{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.hidden{display:none}
.run{width:100%;margin-top:6px;padding:12px;font-family:var(--disp);font-weight:700;letter-spacing:.08em;
   background:var(--live);color:#04141b;border:0;border-radius:8px;cursor:pointer;font-size:14px}
.run:disabled{background:var(--edge);color:var(--cash);cursor:not-allowed}
.progress{margin-top:12px;font-family:var(--mono);font-size:11px;color:var(--cash)}
.progress .bar{height:5px;background:#161c25;border-radius:3px;overflow:hidden;margin-bottom:6px}
.progress .bar i{display:block;height:100%;width:0;background:var(--live);transition:width .25s}
.plate{display:flex;align-items:center;gap:12px;padding:18px;border-radius:9px;border:1px solid var(--edge);
   background:#10151c;font-family:var(--disp);font-size:20px;font-weight:700;letter-spacing:.03em}
.plate .lamp{width:13px;height:13px}
.plate.idle{color:var(--cash);font-family:var(--body);font-weight:400;font-size:15px}
.plate.live{color:var(--live)} .plate.live .lamp{background:var(--live);color:var(--live)}
.plate.parked{color:var(--standby)} .plate.parked .lamp{background:var(--standby);color:var(--standby)}
.plate.powering{animation:flicker .5s ease-out}
@keyframes flicker{0%{opacity:.2}30%{opacity:.9}45%{opacity:.4}100%{opacity:1}}
.gauges{display:flex;gap:18px;margin-top:14px;font-family:var(--mono);font-size:12px;color:var(--cash);flex-wrap:wrap}
.gauges b{display:block;color:var(--paper);font-size:18px}
.chart-head{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
.legend{display:flex;gap:14px;font-family:var(--mono);font-size:11px;color:var(--cash)}
.legend i{display:inline-block;width:22px;height:0;border-top-width:2px;border-top-style:solid;margin-right:5px;vertical-align:middle}
.legend .sw{width:13px;height:11px;border:1px solid var(--edge);display:inline-block;margin-right:5px;vertical-align:middle}
.chart{height:460px;width:100%}
.table{overflow-x:auto;font-family:var(--mono);font-size:12px}
.table table{border-collapse:collapse;width:100%;white-space:nowrap}
.table th,.table td{padding:7px 11px;text-align:right;border-bottom:1px solid #1c2430}
.table th{color:var(--cash);font-weight:500;text-align:right;position:sticky;top:0}
.table td:first-child,.table th:first-child,.table td:nth-child(2),.table th:nth-child(2){text-align:left}
.table tr:first-child td{color:var(--live)}
.muted{color:var(--cash)}
.neg{color:var(--drawdown)}
@media(max-width:820px){.grid{grid-template-columns:1fr;
   grid-template-areas:"console" "verdict" "chart" "backups"}.chart{height:380px}}
@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
```

- [ ] **Step 3: Verify it loads** — `cd src && FEIYANG_DEV_FIXTURE=1 python3 -m uvicorn webapp.app:app --port 8000`, open `http://localhost:8000`, confirm the layout, fonts, panels, and the warming/ready lamp render (chart empty until Task 9). Stop the server.

- [ ] **Step 4: Commit**

```bash
git add src/webapp/static/index.html src/webapp/static/styles.css
git commit -m "feat(ui): standby-console SPA shell + theme"
```

---

## Task 9: Frontend logic — vendor ECharts, scan/SSE client, table, 3-panel chart

**Files:**
- Create: `src/webapp/static/vendor/echarts.min.js` (downloaded), `src/webapp/static/app.js`
- Test: manual + optional Playwright smoke (example-skills:webapp-testing)

**Interfaces:**
- Consumes API endpoints from Task 5; renders into the DOM from Task 8.

- [ ] **Step 1: Vendor ECharts 6.1.0**

Run: `mkdir -p src/webapp/static/vendor && curl -fsSL https://cdn.jsdelivr.net/npm/echarts@6.1.0/dist/echarts.min.js -o src/webapp/static/vendor/echarts.min.js`
Expected: a ~1 MB file. Verify: `head -c 200 src/webapp/static/vendor/echarts.min.js` shows the ECharts banner mentioning 6.1.0.

- [ ] **Step 2: Create `src/webapp/static/app.js`:**

```javascript
"use strict";
const $ = (id) => document.getElementById(id);
const COLORS = { live: "#2BB8E6", standby: "#F2A93B", cash: "#8A93A3",
                 drawdown: "#E8806B", paper: "#ECEFF3", edge: "#38424F" };
const REGIME_COLOR = { primary: COLORS.live, candidate: COLORS.standby, cash: COLORS.cash };
const REDUCE = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const pct = (x) => (x * 100).toFixed(1) + "%";
let chart;

// ---- strategy field toggle ----
function syncRuleFields() {
  const cross = $("rule").value === "ma_cross";
  $("cross-fields").classList.toggle("hidden", !cross);
  $("ma-field").classList.toggle("hidden", cross);
}
$("rule").addEventListener("change", syncRuleFields);

// ---- cache status poll ----
async function pollStatus() {
  try {
    const s = await (await fetch("/api/status")).json();
    const lamp = $("cache-lamp").querySelector(".lamp");
    if (s.state === "ready") {
      lamp.className = "lamp ready";
      $("cache-text").textContent = `cache READY · ${s.total || ""}`.trim();
      $("run").disabled = false;
    } else {
      lamp.className = "lamp warming";
      $("cache-text").textContent = `Warming the cache — ${s.loaded} / ${s.total}`;
      $("run").disabled = true;
      setTimeout(pollStatus, 1500);
    }
  } catch { setTimeout(pollStatus, 2000); }
}

// ---- build request body ----
function buildBody() {
  const rule = $("rule").value;
  const body = { ticker: $("ticker").value.trim().toUpperCase(), rule,
                 mode: $("mode").value, sort: $("sort").value };
  if (rule === "ma_cross") { body.fast = +$("fast").value; body.slow = +$("slow").value; }
  else { body.ma = +$("ma").value; }
  return body;
}

// ---- run a scan ----
$("run").addEventListener("click", runScan);
async function runScan() {
  const body = buildBody();
  if (!body.ticker) { setPlate("idle", "Pick a ticker and run a scan."); return; }
  $("run").disabled = true;
  $("progress").classList.remove("hidden");
  setProgress(0, 0);
  let res;
  try {
    res = await fetch("/api/scan", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  } catch { return failScan("Network error — try again."); }
  if (res.status === 422) return failScan("Check the strategy params (e.g. fast < slow).");
  if (res.status === 503) return failScan("Warming the cache — try again shortly.");
  if (!res.ok) return failScan("Scan failed to start.");
  const { job_id } = await res.json();
  streamJob(job_id);
}

function streamJob(jobId) {
  const ev = new EventSource(`/api/scan/${jobId}/events`);
  ev.addEventListener("progress", (m) => {
    const d = JSON.parse(m.data); setProgress(d.done, d.total);
  });
  let finished = false;
  ev.addEventListener("result", async (m) => {
    finished = true;
    ev.close();
    const d = JSON.parse(m.data);
    $("progress").classList.add("hidden");
    $("run").disabled = false;
    if (d.status !== "done") return failScan(d.error || "Scan did not complete.");
    try {                                            // gzip-eligible JSON poll
      const full = await (await fetch(`/api/scan/${jobId}/result`)).json();
      render(full.result);
    } catch { failScan("Could not load the result — please re-run."); }
  });
  ev.addEventListener("error", () => {
    if (finished || ev.readyState === EventSource.CLOSED) return;  // ignore post-result close
    ev.close();
    failScan("Service restarted — please re-run.");
  });
}

function failScan(msg) {
  $("progress").classList.add("hidden");
  $("run").disabled = false;
  setPlate("idle", msg);
}
function setProgress(done, total) {
  const frac = total ? done / total : 0;
  $("progress").querySelector("i").style.width = (frac * 100).toFixed(1) + "%";
  $("progress").querySelector(".readout").textContent =
    `SCANNING ${done.toLocaleString()} / ${total.toLocaleString()}`;
}
function setPlate(kind, text) {
  const p = $("state-plate");
  p.className = "plate " + kind + (REDUCE ? "" : " powering");
  p.querySelector(".plate-text").textContent = text;
}

// ---- render results ----
function render(r) {
  const rec = r.recommendation;
  if (rec.state === "in-market") setPlate("live", `LIVE — holding ${r.curves.picks.length ? document.getElementById("ticker").value.trim().toUpperCase() : ""}`.trim());
  else setPlate("parked", rec.top ? `STANDBY — park in ${rec.top}` : "STANDBY — no qualifying backup");
  renderGauges(r.ranked[0]);
  renderTable(r.ranked);
  renderChart(r.curves);
}

function renderGauges(top) {
  if (!top) { $("gauges").innerHTML = ""; return; }
  $("gauges").innerHTML =
    `<div>afscore<b>${(top.afscore * 100).toFixed(1)}</b></div>
     <div>corr_off<b>${top.corr_off.toFixed(2)}</b></div>
     <div>park%<b>${(top.park_return * 100).toFixed(0)}</b></div>`;
}

function renderTable(rows) {
  if (!rows.length) { $("table").innerHTML = `<p class="muted">No qualifying backup.</p>`; return; }
  const cols = [["#", (r, i) => i + 1], ["ticker", (r) => r.ticker],
    ["off%", (r) => (r.off_frac * 100).toFixed(0)], ["afscore", (r) => (r.afscore * 100).toFixed(1)],
    ["cagr_n", (r) => pct(r.cagr_n)], ["maxdd_n", (r) => pct(r.max_dd_n)],
    ["calmar_n", (r) => r.calmar_n.toFixed(2)], ["sharpe_n", (r) => r.sharpe_n.toFixed(2)],
    ["corr", (r) => r.corr_off.toFixed(2)], ["park%", (r) => (r.park_return * 100).toFixed(0)],
    ["cagr_f", (r) => pct(r.cagr_f)], ["maxdd_f", (r) => pct(r.max_dd_f)]];
  const head = "<tr>" + cols.map((c) => `<th>${c[0]}</th>`).join("") + "</tr>";
  const body = rows.map((r, i) => "<tr>" + cols.map((c) => `<td>${c[1](r, i)}</td>`).join("") + "</tr>").join("");
  $("table").innerHTML = `<table>${head}${body}</table>`;
}

// ---- 3-panel chart: equity (log) / regime ribbon / drawdown ----
function renderChart(curves) {
  if (!chart) chart = echarts.init($("chart"), null, { renderer: "canvas" });
  const dates = curves.dates;
  // Regime ribbon: look up fill/pattern by dataIndex — api.value() on a value axis
  // coerces the state string to null, so we never read state from api. Each state
  // also carries a non-color channel (solid cyan / amber+hatch / grey dotted-empty)
  // for colorblind separability per the design's redundant-encoding rule.
  const dateIdx = new Map(dates.map((d, i) => [d, i]));
  const regimeRows = curves.regime.map((s) => ({ value: [dateIdx.get(s.start), dateIdx.get(s.end)] }));
  const regimeStates = curves.regime.map((s) => s.state);
  const renderRibbon = (params, api) => {
    const x0 = api.coord([api.value(0), 0])[0];
    const x1 = api.coord([api.value(1), 0])[0];
    const cs = params.coordSys;
    const x = Math.min(x0, x1), w = Math.max(1, Math.abs(x1 - x0));
    const rect = { x, y: cs.y, width: w, height: cs.height };
    const state = regimeStates[params.dataIndex];
    if (state === "primary")
      return { type: "rect", shape: rect, style: { fill: REGIME_COLOR.primary } };
    if (state === "cash")                            // empty + dotted outline
      return { type: "rect", shape: rect,
        style: { fill: "transparent", stroke: REGIME_COLOR.cash, lineWidth: 1, lineDash: [2, 2] } };
    const hatch = [];                                // candidate: amber + diagonal hatch
    for (let hx = x - cs.height; hx < x + w; hx += 6)
      hatch.push({ type: "line",
        shape: { x1: hx, y1: cs.y + cs.height, x2: hx + cs.height, y2: cs.y },
        style: { stroke: "#10151c", lineWidth: 1 } });
    return { type: "group", clipPath: { type: "rect", shape: rect },
      children: [{ type: "rect", shape: rect, style: { fill: REGIME_COLOR.candidate } }, ...hatch] };
  };
  const lineStyles = ["solid", "dashed", "dotted"];
  const pickSeries = curves.picks.map((p, i) => ({
    name: `+ ${p.ticker}`, type: "line", xAxisIndex: 0, yAxisIndex: 0,
    data: p.equity, showSymbol: false,
    lineStyle: { width: i === 0 ? 2.4 : 1.4, type: lineStyles[(i + 2) % 3] },
    color: i === 0 ? COLORS.live : COLORS.cash }));
  const baseSeries = [
    { name: "Primary Buy&Hold", type: "line", xAxisIndex: 0, yAxisIndex: 0,
      data: curves.primary_buy_hold, showSymbol: false,
      lineStyle: { width: 1.2, color: COLORS.paper } },
    { name: "Primary + cash", type: "line", xAxisIndex: 0, yAxisIndex: 0,
      data: curves.primary_cash, showSymbol: false,
      lineStyle: { width: 1, type: "dashed", color: COLORS.cash } }];

  chart.setOption({
    animation: !REDUCE, animationDuration: 700, backgroundColor: "transparent",
    textStyle: { fontFamily: "Spline Sans Mono, monospace", color: COLORS.cash },
    grid: [{ left: 56, right: 18, top: 16, height: 250 },
           { left: 56, right: 18, top: 280, height: 26 },
           { left: 56, right: 18, top: 326, height: 92 }],
    axisPointer: { link: [{ xAxisIndex: "all" }], lineStyle: { color: COLORS.edge } },
    tooltip: { trigger: "axis", axisPointer: { type: "cross" },
      backgroundColor: "#10151c", borderColor: COLORS.edge,
      textStyle: { color: COLORS.paper, fontFamily: "Spline Sans Mono, monospace" } },
    xAxis: [
      { type: "category", data: dates, gridIndex: 0, axisLabel: { show: false }, axisLine: { lineStyle: { color: COLORS.edge } } },
      { type: "category", data: dates, gridIndex: 1, axisLabel: { show: false }, axisTick: { show: false }, axisLine: { show: false } },
      { type: "category", data: dates, gridIndex: 2, axisLine: { lineStyle: { color: COLORS.edge } } }],
    yAxis: [
      { type: "log", gridIndex: 0, name: "equity", splitLine: { lineStyle: { color: "#1b2330" } } },
      { type: "value", gridIndex: 1, show: false, min: 0, max: 1 },
      { type: "value", gridIndex: 2, name: "DD %", splitLine: { lineStyle: { color: "#1b2330" } } }],
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1, 2] },
      { type: "slider", xAxisIndex: [0, 1, 2], bottom: 4, height: 14,
        borderColor: COLORS.edge, fillerColor: "rgba(43,184,230,.15)" }],
    series: [
      ...baseSeries, ...pickSeries,
      { name: "regime", type: "custom", xAxisIndex: 1, yAxisIndex: 1, clip: true,
        renderItem: renderRibbon, encode: { x: [0, 1] }, data: regimeRows,
        tooltip: { formatter: (p) => `regime: ${regimeStates[p.dataIndex]}` } },
      { name: "rank-1 DD", type: "line", xAxisIndex: 2, yAxisIndex: 2,
        data: curves.rank1_drawdown, showSymbol: false, areaStyle: { opacity: 0.25 },
        lineStyle: { width: 1, color: COLORS.drawdown }, color: COLORS.drawdown }],
  }, true);
  renderLegend(curves);
}

function renderLegend(curves) {
  const items = [
    `<span><i style="border-top-color:${COLORS.paper}"></i>Primary B&H</span>`,
    `<span><i style="border-top-color:${COLORS.cash};border-top-style:dashed"></i>Primary+cash</span>`,
    ...curves.picks.map((p, i) =>
      `<span><i style="border-top-color:${i === 0 ? COLORS.live : COLORS.cash};border-top-style:${["solid","dashed","dotted"][(i+2)%3]}"></i>${p.ticker}</span>`),
    `<span><b class="sw" style="background:${COLORS.live}"></b>LIVE</span>`,
    `<span><b class="sw" style="background:${COLORS.standby}"></b>PARKED</span>`,
    `<span><b class="sw" style="background:${COLORS.cash}"></b>CASH</span>`];
  $("legend").innerHTML = items.join("");
}

window.addEventListener("resize", () => chart && chart.resize());
window.matchMedia("(prefers-reduced-motion: reduce)").addEventListener("change", () => {
  if (chart) chart.setOption({ animation: !window.matchMedia("(prefers-reduced-motion: reduce)").matches });
});

syncRuleFields();
pollStatus();
```

- [ ] **Step 3: Manual verification** — `cd src && FEIYANG_DEV_FIXTURE=1 python3 -m uvicorn webapp.app:app --port 8000`, open `http://localhost:8000`:
  - ticker `DEMO`, strategy `MA cross`, fast 5 / slow 20, click **RUN SCAN**.
  - Expect: progress readout runs, then the state plate powers on (LIVE/STANDBY), gauges fill, the backups table lists `GLD/QQQ/SPY/TLT`, and the chart shows the log equity lines, the regime ribbon (cyan/amber/grey bands), and the clay drawdown panel. Hover shows a synced crosshair tooltip; the slider zooms all panels together. Stop the server.

- [ ] **Step 4: Optional Playwright smoke** (if the example-skills:webapp-testing toolkit is available): start the dev server, navigate, click `#run`, wait for `#table table`, assert the `#chart canvas` exists and `#state-plate` is `live` or `parked`. Capture a screenshot for the record.

- [ ] **Step 5: Commit**

```bash
git add src/webapp/static/vendor/echarts.min.js src/webapp/static/app.js
git commit -m "feat(ui): ECharts 3-panel chart, SSE scan client, ranked table"
```

---

## Task 10: Dockerfile + local container smoke

**Files:**
- Create: `Dockerfile`, `.dockerignore`

**Interfaces:** produces an image whose `CMD` runs a single uvicorn worker serving `webapp.app:app` from `WORKDIR /app/src`.

- [ ] **Step 1: Create `.dockerignore`:**

```
.git
data
**/__pycache__
*.pyc
docs
deploy/.env
```

- [ ] **Step 2: Create `Dockerfile`:**

```dockerfile
# feiyangyang web service — single-worker FastAPI on Fargate.
FROM python:3.12-slim

# Non-root runtime user.
RUN useradd --create-home --uid 10001 app
WORKDIR /app

# Install deps first for layer caching.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# App source. data_loader writes the cache under /app/data/ohlc at warmup.
COPY src/ /app/src/
RUN mkdir -p /app/data/ohlc && chown -R app:app /app
USER app

WORKDIR /app/src
EXPOSE 8080
# SINGLE worker — uvicorn defaults to one in-process worker; do NOT pass --workers
# (even =1 spawns a supervisor) so the ~1GB warm cache + job registry stay single-process.
CMD ["uvicorn", "webapp.app:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
```

- [ ] **Step 3: Build**

Run: `docker build -t feiyangyang:dev .`
Expected: build succeeds.

- [ ] **Step 4: Run with the dev fixture (no AWS needed) and smoke**

Run:
```bash
docker run --rm -d -p 8080:8080 -e FEIYANG_DEV_FIXTURE=1 --name fy feiyangyang:dev
sleep 4
curl -s localhost:8080/healthz                       # -> ok
curl -s localhost:8080/api/status                     # -> {"state":"ready",...}
curl -s -X POST localhost:8080/api/scan -H 'Content-Type: application/json' \
  -d '{"ticker":"DEMO","rule":"ma_cross","fast":5,"slow":20,"top_k":2}'   # -> {"job_id":...}
docker stop fy
```
Expected: `/healthz` returns `ok`; `/api/status` ready; scan returns a `job_id`.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "build: single-worker FastAPI Dockerfile (python:3.12-slim)"
```

---

## Task 11: Deploy artifacts — task definition, IAM, deploy.sh, wake.sh

**Files:**
- Create: `deploy/task-definition.json`, `deploy/iam/task-role-policy.json`, `deploy/iam/trust-policy.json`, `deploy/deploy.sh`, `deploy/wake.sh`, `deploy/.env.example`, `deploy/README.md`

**Interfaces:** none (ops config). Placeholders use `${VAR}` resolved by `deploy.sh` from `deploy/.env`.

- [ ] **Step 1: `deploy/.env.example`:**

```bash
# Copy to deploy/.env and fill in. deploy.sh sources this.
AWS_REGION=ap-northeast-1
AWS_ACCOUNT_ID=000000000000
ECR_REPO=feiyangyang
ECS_CLUSTER=ff
ECS_SERVICE=feiyangyang
IMAGE_TAG=latest
# Build architecture — CPU_ARCH must equal runtimePlatform.cpuArchitecture in the
# task def, and DOCKER_PLATFORM must match the build. This host is aarch64, so ARM64
# (Fargate Graviton, available in ap-northeast-1) builds natively. For x86 set
# CPU_ARCH=X86_64 and DOCKER_PLATFORM=linux/amd64 (needs buildx/QEMU on an arm host).
CPU_ARCH=ARM64
DOCKER_PLATFORM=linux/arm64
# Subnets/SG for the awsvpc task (SG must allow the ALB SG inbound on 8080).
SUBNETS=subnet-aaaa,subnet-bbbb
SECURITY_GROUP=sg-cccc
# Existing ALB target group for ff.theblueprint.xyz (containerPort 8080, target type ip).
TARGET_GROUP_ARN=arn:aws:elasticloadbalancing:ap-northeast-1:000000000000:targetgroup/ff/xxxx
# Role ARNs (see deploy/README.md to create them).
EXECUTION_ROLE_ARN=arn:aws:iam::000000000000:role/feiyangyang-exec
TASK_ROLE_ARN=arn:aws:iam::000000000000:role/feiyangyang-task
ASSIGN_PUBLIC_IP=DISABLED
```

- [ ] **Step 2: `deploy/iam/trust-policy.json`** (shared trust for both roles):

```json
{
  "Version": "2012-10-17",
  "Statement": [{ "Effect": "Allow",
    "Principal": { "Service": "ecs-tasks.amazonaws.com" },
    "Action": "sts:AssumeRole" }]
}
```

- [ ] **Step 3: `deploy/iam/task-role-policy.json`** (app identity — S3 read + self idle-stop):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "ReadOhlcObjects", "Effect": "Allow", "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::staking-ledger-bpt/jojo_quant/ohlc/*",
      "Condition": { "StringEquals": { "aws:ResourceAccount": "${AWS_ACCOUNT_ID}" } } },
    { "Sid": "ListOhlcPrefix", "Effect": "Allow", "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::staking-ledger-bpt",
      "Condition": { "StringLike": { "s3:prefix": "jojo_quant/ohlc/*" } } },
    { "Sid": "IdleAutoStopSelf", "Effect": "Allow",
      "Action": ["ecs:UpdateService", "ecs:DescribeServices"],
      "Resource": "arn:aws:ecs:${AWS_REGION}:${AWS_ACCOUNT_ID}:service/${ECS_CLUSTER}/${ECS_SERVICE}" }
  ]
}
```

- [ ] **Step 4: `deploy/task-definition.json`** (registered by deploy.sh after `envsubst`):

```json
{
  "family": "feiyangyang",
  "requiresCompatibilities": ["FARGATE"],
  "networkMode": "awsvpc",
  "cpu": "2048",
  "memory": "8192",
  "runtimePlatform": { "cpuArchitecture": "${CPU_ARCH}", "operatingSystemFamily": "LINUX" },
  "executionRoleArn": "${EXECUTION_ROLE_ARN}",
  "taskRoleArn": "${TASK_ROLE_ARN}",
  "containerDefinitions": [
    {
      "name": "web",
      "image": "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}:${IMAGE_TAG}",
      "essential": true,
      "stopTimeout": 120,
      "portMappings": [{ "containerPort": 8080, "protocol": "tcp" }],
      "environment": [
        { "name": "ECS_CLUSTER", "value": "${ECS_CLUSTER}" },
        { "name": "ECS_SERVICE", "value": "${ECS_SERVICE}" }
      ],
      "healthCheck": {
        "command": ["CMD-SHELL", "python3 -c \"import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/healthz').status==200 else 1)\""],
        "interval": 30, "timeout": 5, "retries": 3, "startPeriod": 300
      },
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/feiyangyang",
          "awslogs-region": "${AWS_REGION}",
          "awslogs-stream-prefix": "ecs"
        }
      }
    }
  ]
}
```

- [ ] **Step 5: `deploy/deploy.sh`:**

```bash
#!/usr/bin/env bash
# Build -> push to ECR -> register task def -> create/update the ECS service.
# Requires AWS CLI v2 with credentials, and a filled deploy/.env.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
set -a; source deploy/.env; set +a

command -v envsubst >/dev/null || { echo "Install gettext (provides envsubst)"; exit 1; }

REPO_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}"

echo "==> Login to ECR"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
aws ecr describe-repositories --repository-names "$ECR_REPO" --region "$AWS_REGION" >/dev/null 2>&1 \
  || aws ecr create-repository --repository-name "$ECR_REPO" --region "$AWS_REGION" >/dev/null

echo "==> Build & push ${REPO_URI}:${IMAGE_TAG} (${DOCKER_PLATFORM})"
docker build --platform "${DOCKER_PLATFORM}" -t "${REPO_URI}:${IMAGE_TAG}" .
docker push "${REPO_URI}:${IMAGE_TAG}"

echo "==> Ensure log group exists (awslogs driver won't create it without logs:CreateLogGroup)"
aws logs create-log-group --log-group-name /ecs/feiyangyang --region "$AWS_REGION" 2>/dev/null || true

echo "==> Register task definition"
TD=$(envsubst < deploy/task-definition.json)
TASK_DEF_ARN=$(aws ecs register-task-definition --region "$AWS_REGION" \
  --cli-input-json "$TD" --query 'taskDefinition.taskDefinitionArn' --output text)
echo "    $TASK_DEF_ARN"

NET="awsvpcConfiguration={subnets=[${SUBNETS}],securityGroups=[${SECURITY_GROUP}],assignPublicIp=${ASSIGN_PUBLIC_IP}}"
LB="targetGroupArn=${TARGET_GROUP_ARN},containerName=web,containerPort=8080"

if aws ecs describe-services --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
     --region "$AWS_REGION" --query 'services[0].status' --output text 2>/dev/null | grep -q ACTIVE; then
  echo "==> Update existing service"
  aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
    --task-definition "$TASK_DEF_ARN" --desired-count 1 --region "$AWS_REGION" >/dev/null
else
  echo "==> Create service (behind the ff.theblueprint.xyz target group)"
  aws ecs create-service --cluster "$ECS_CLUSTER" --service-name "$ECS_SERVICE" \
    --task-definition "$TASK_DEF_ARN" --desired-count 1 --launch-type FARGATE \
    --health-check-grace-period-seconds 600 \
    --deployment-configuration "minimumHealthyPercent=0,maximumPercent=100" \
    --network-configuration "$NET" --load-balancers "$LB" --region "$AWS_REGION" >/dev/null
fi
echo "==> Done. Watch: aws ecs describe-services --cluster $ECS_CLUSTER --services $ECS_SERVICE --region $AWS_REGION"
```

- [ ] **Step 6: `deploy/wake.sh`** (idle-stop wake path — the app can't wake itself):

```bash
#!/usr/bin/env bash
# Wake the service after idle auto-stop set desiredCount=0.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
set -a; source deploy/.env; set +a
aws ecs update-service --cluster "$ECS_CLUSTER" --service "$ECS_SERVICE" \
  --desired-count 1 --region "$AWS_REGION" >/dev/null
echo "Waking $ECS_SERVICE — allow 1-3 min for cache warmup, then load ff.theblueprint.xyz"
```

- [ ] **Step 7: `deploy/README.md`** — write a concise runbook:

```markdown
# Deploy feiyangyang to ECS Fargate (ap-northeast-1, cluster `ff`)

Personal, no-auth service fronted by the existing **ff.theblueprint.xyz** ALB.

## Prerequisites (confirm these exist)
- AWS CLI v2, Docker (with buildx/QEMU if cross-building arches), and `envsubst`
  (from gettext — macOS: `brew install gettext`). `CPU_ARCH` / `DOCKER_PLATFORM` in
  `.env` must match each other and the build host (this host is aarch64 → ARM64).
- ECS cluster `ff` in `ap-northeast-1`.
- An ALB + HTTPS listener terminating `ff.theblueprint.xyz`, with a host rule
  routing to a **target group** (`target-type: ip`, port 8080, health check `/healthz`).
  Put its ARN in `TARGET_GROUP_ARN`.
- The OHLC S3 bucket `staking-ledger-bpt` (prefix `jojo_quant/ohlc/`) readable from
  this account.

## One-time IAM
```bash
set -a; source deploy/.env; set +a
# Execution role (ECR pull + logs)
aws iam create-role --role-name feiyangyang-exec \
  --assume-role-policy-document file://deploy/iam/trust-policy.json
aws iam attach-role-policy --role-name feiyangyang-exec \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
# Task role (S3 read + self idle-stop)
aws iam create-role --role-name feiyangyang-task \
  --assume-role-policy-document file://deploy/iam/trust-policy.json
envsubst < deploy/iam/task-role-policy.json > /tmp/task-role-policy.json
aws iam put-role-policy --role-name feiyangyang-task \
  --policy-name feiyangyang-s3-ecs --policy-document file:///tmp/task-role-policy.json
```
Put the resulting role ARNs in `deploy/.env`.

## Deploy
```bash
cp deploy/.env.example deploy/.env   # fill in the blanks
bash deploy/deploy.sh
```
First deploy `create-service`s with `healthCheckGracePeriodSeconds=600` so the
1-3 min cold-start warmup isn't health-checked to death; later deploys
`update-service`. Open https://ff.theblueprint.xyz once `/api/status` is `ready`.

## Cost / idle auto-stop
Always-on 2 vCPU / 8 GB ≈ ~$85/mo. To save ~90%, in the UI set an idle window
(`POST /api/idle-policy {minutes}`); the app drops `desiredCount` to 0 when idle.
Wake it next session with `bash deploy/wake.sh` (the app can't wake itself).
```

- [ ] **Step 8: Validate the JSON + shell** (no AWS calls):

Run:
```bash
python3 -c "import json;[json.load(open(p)) for p in ['deploy/task-definition.json','deploy/iam/task-role-policy.json','deploy/iam/trust-policy.json']];print('json ok')"
bash -n deploy/deploy.sh && bash -n deploy/wake.sh && echo "shell ok"
chmod +x deploy/deploy.sh deploy/wake.sh
```
Expected: `json ok` and `shell ok`. (`task-definition.json` / `task-role-policy.json` contain `${VAR}` placeholders — `json.load` still parses them as strings; `deploy.sh` resolves them via `envsubst` at deploy time.)

- [ ] **Step 9: Commit**

```bash
git add deploy/
git commit -m "build(deploy): ECS Fargate task def, IAM, deploy/wake scripts + runbook"
```

---

## Task 12: Documentation — README.md + CLAUDE.md

**Files:**
- Modify: `README.md`, `CLAUDE.md`

- [ ] **Step 1: Update `README.md`** — add a "Web service" section after the CLI usage. Insert:

```markdown
## Web service (STANDBY CONSOLE)

A personal FastAPI app over the same engine: enter a ticker, pick a strategy, set
params, run a background full-market scan with live progress, and read the ranked
backups plus an interactive ECharts equity/regime/drawdown chart.

### Run locally
```bash
pip install -r requirements.txt
# Offline demo (synthetic data, no cache/S3 needed):
cd src && FEIYANG_DEV_FIXTURE=1 python3 -m uvicorn webapp.app:app --port 8000
# Real data (needs the OHLC cache; pulled from S3 at startup, or pre-pull):
bash scripts/pull_cache.sh
cd src && python3 -m uvicorn webapp.app:app --port 8000
```
Open http://localhost:8000. Run the server with `src/` as the working dir (bare
imports), and **a single worker only** — the ~1 GB warm cache and the in-memory
job registry must live in one process.

### Endpoints
`GET /healthz` · `GET /api/status` · `GET /api/universe` · `POST /api/scan` →
`{job_id}` · `GET /api/scan/{id}/events` (SSE progress + result) ·
`GET /api/scan/{id}/result` (poll) · `GET|POST /api/idle-policy`.

### Tests
```bash
python3 src/test_webapp.py   # web layer (needs the deps; no cache)
python3 src/test_rebound.py  # engine (no deps beyond pandas/numpy)
```

### Deploy (ECS Fargate, ap-northeast-1, cluster `ff`, ALB ff.theblueprint.xyz)
See `deploy/README.md`. Container syncs the cache from S3 at startup, warms ~1 GB
into RAM (1-3 min cold start), runs 2 vCPU / 8 GB always-on; an opt-in idle
auto-stop toggle drops it to `desiredCount=0` to save cost (wake via
`deploy/wake.sh`).
```

  Also add to the file-map table (the `| scripts/pull_cache.sh | ... |` table): rows for `src/webapp/` (FastAPI web service), `Dockerfile`, and `deploy/`.

- [ ] **Step 2: Update `CLAUDE.md`** — under "Architecture", add a subsection:

```markdown
### Web service (`src/webapp/`)

A FastAPI layer over the engine (no engine behavior change). `app.py` (lifespan
warmup + routes + SSE + static mount), `engine_service.py` (warm in-memory frame
store + `run_scan` over the engine's pure functions, bypassing `scan()`'s disk
re-read), `jobs.py` (single-thread background scan registry), `cache_sync.py`
(boto3 S3 → disk), `idle.py` (opt-in idle auto-stop), `static/` (ECharts SPA).
Run as `uvicorn webapp.app:app` from `src/`, **single worker** (the warm cache +
job registry are process-local). Engine reuse must exclude the primary from
candidates, load the primary on demand, and build rule-specific params (the
`curve_series`/`evaluate_iter` helpers are additive in `rebound.py`). Tests:
`src/test_webapp.py` (hand-rolled, no cache). Dev/offline: `FEIYANG_DEV_FIXTURE=1`.
```

  Also update the "Commands" block to mention `python3 src/test_webapp.py` and the dev-fixture run command.

- [ ] **Step 3: Verify docs match reality**

Run: `python3 src/test_rebound.py && python3 src/test_webapp.py`
Expected: both suites pass (22 and 21). Re-read the new README/CLAUDE sections against the actual endpoints and run commands.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: document the web service (run, endpoints, tests, deploy)"
```

---

## Self-Review

**Spec coverage** (each spec section → task):
- §3 module layout → File Structure + Tasks 2–9.
- §4 engine-reuse contract → Task 1 (`evaluate_iter`/`curve_series`), Task 2 (params validation), Task 3 (exclude primary, on-demand primary, conversions). ✓
- §5.1 lifespan warmup → Task 5 (`lifespan`, background thread, `STATE`). ✓
- §5.2 job model (threadpool, single scan, batched progress, cancel) → Task 4 + Task 3 (`_PROGRESS_EVERY=50`, `cancel_event`). ✓
- §5.3 endpoints → Task 5 (+ idle-policy Task 7). ✓
- §5.4 schemas → Task 2. ✓
- §6 frontend (palette, type, signature ribbon, no markArea, motion, copy, a11y) → Tasks 8–9. ✓
- §7 `curve_series` shape/no-renorm/regime → Task 1. ✓
- §8 ECharts 3-panel/log/markArea-cut/crosshair/dataZoom → Task 9. ✓
- §9 perf (batched progress, gzip) → Task 3 batching; gzip note below. ⚠ see gap.
- §10 deployment (Dockerfile single worker, task def 2vCPU/8GB, IAM two roles, health, ALB, idle) → Tasks 10–11. ✓
- §10.6/§11 idle auto-stop + ALB + size → Task 7 + Task 11. ✓
- §13 testing → every task's TDD + Task 9 smoke. ✓
- §14 doc-sync → Task 12. ✓

### Adversarial plan review — fixes applied (2026-06-26)

A 4-reviewer workflow (spec-coverage/types, backend code, frontend/ECharts, deploy config) — with reviewers reconstructing Tasks 1–7 in `/tmp` and running the suites — found 5 blockers, all now patched in the task code above:

1. **plot_curves refactor** (Task 1 Step 6) restored the `fig, (ax1, ax2) = plt.subplots(...)` line the old span contained (else `NameError`, `test_plot_curves` fails). Dropped the dead `picks_n` line.
2. **engine_service exclude-primary test** (Task 3 Step 1) now passes `min_history=0` so the 399-bar fixtures survive the 5-year (1260-bar) history filter (else `ranked` is empty → `IndexError`).
3. **Pydantic PEP 604 unions** → `typing.Optional[...]` (Task 2, Task 7) so the models import on this worktree's Python 3.9 (`int | None` crashes pydantic v2 at import there).
4. **Regime ribbon color** (Task 9): look up fills by `params.dataIndex` (not `api.value(2)`, which a value axis coerces to `null` → colorless ribbon); dropped `encode y:2`; added `clip:true`.
5. **awslogs log group** (Task 11): `deploy.sh` pre-creates `/ecs/feiyangyang` and the task def drops `awslogs-create-group` (the managed exec-role policy lacks `logs:CreateLogGroup`, so the first container would never start). **Image arch** parameterized via `CPU_ARCH`/`DOCKER_PLATFORM` (build host is aarch64; X86_64 task def + bare build = exec-format failure).

Majors/minors also applied: §9 **gzip** is now in the Task 5 `app.py` code (`GZipMiddleware`), and the ~49 KB curve payload is delivered over the **gzip-eligible `GET /result`** poll (SSE `result` now signals completion only, keeping the live progress stream small); the **regime ribbon carries a non-color channel** (solid cyan / amber+hatch / grey dotted-empty) per the colorblind rule; Dockerfile `CMD` dropped `--workers 1`; task def gained `stopTimeout:120`; `belongs_to_epoch` is now wired into `/result` + SSE; pick colors agree between chart and legend; the SSE client ignores native errors after completion; `deploy.sh` guards on `envsubst`; the dead `s3:GetBucketLocation` grant and an over-tight history default were removed/clarified.

**Intentionally out of scope (YAGNI):** §5.2's "distinguish skipped-thin vs errored candidates" — `evaluate_iter` keeps the 2-tuple `(ticker, row_or_None)`; for a personal single-user tool the progress count tolerates folding the (already-rare, engine-swallowed) errors into skips. Revisit only if error diagnosis on the SSE stream is wanted.

**Placeholder scan:** no TBD/TODO; every code step has complete code; deploy `${VAR}` placeholders are intentional and resolved by `envsubst` in `deploy.sh` (documented in Task 11 Step 8). ✓

**Type consistency:** `ScanRequest.{params,max_dd_cap,min_history_bars}` defined in Task 2 are used unchanged in Task 3; `run_scan(req, *, progress_cb, cancel_event)` defined in Task 3 is called identically in Task 4; `JobState.{status,done,total,result,error}` from Task 4 is read identically in Task 5's `/result` and SSE; `curve_series` keys from Task 1 (`dates, primary_buy_hold, primary_cash, picks[].equity, rank1_drawdown, regime[].{start,end,state}`) are consumed verbatim in Task 9's `renderChart`. `STATE["server_epoch"]` (Task 5) feeds `jobs.submit(req, server_epoch)` (Task 4) and the 410 body (Task 5). ✓

---

## Execution notes

- Work happens in the current paseo worktree (`romantic-piranha` branch) — isolation already in place; no new worktree needed.
- Order is dependency-correct: Task 1 (engine) → 2 (schema) → 3 (service) → 4 (jobs) → 5 (app) → 6 (cache_sync) → 7 (idle) → 8–9 (frontend) → 10 (docker) → 11 (deploy) → 12 (docs). Tasks 6 and 7 can swap; both precede a real (non-fixture) prod run.
- Third-party specifics in this plan were verified against official docs on 2026-06-26 (see spec §15). Re-verify `curl`/CLI flags against current docs if a step errors.
```

