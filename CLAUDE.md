# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**feiyangyang (沸羊羊 / 备胎 finder)** ranks "parking" assets to hold while a primary
stock's hold/flat moving-average rule is out of the market, then plots the stitched
equity curve. It is a single-purpose CLI tool extracted from the `jojo_quant` project;
it shares that project's OHLC cache but has no code dependency on it.

## Commands

```bash
pip install -r requirements.txt        # pandas, numpy, pyarrow, matplotlib + FastAPI stack
pip install -r requirements.txt -r requirements-dev.txt  # also installs httpx2 for test_webapp.py
python3 src/test_rebound.py            # engine test suite (22 assert-based cases, no cache needed)
python3 src/test_webapp.py             # web layer test suite (22 assert-based cases, no cache; needs ≥Python 3.10)
bash scripts/pull_cache.sh            # populate data/ohlc/ from S3 (needed only for real runs)
python3 src/rebound.py TSLA --rule ma_cross --fast 5 --slow 30   # end-to-end run
python3 src/rebound.py TSLA --rule ma_cross --fast 5 --slow 30 --limit 30  # smoke test (first 30 candidates)
# Web service (offline/dev mode, no cache needed):
cd src && FEIYANG_DEV_FIXTURE=1 python3 -m uvicorn webapp.app:app --port 8000
# Web service (real data):
cd src && python3 -m uvicorn webapp.app:app --port 8000
```

Run a **single test** (there is no pytest — `test_rebound.py` is a hand-rolled runner):

```bash
python3 -c "import sys; sys.path.insert(0,'src'); import test_rebound as t; t.test_no_lookahead_future_invariance()"
```

**Import model:** all modules use bare imports (`from rebound import ...`,
`import data_loader`) that resolve only because Python puts the script's own directory
(`src/`) on `sys.path[0]`. Always run as `python3 src/<file>.py` from the repo root — not
as `python3 -m` and not from inside `src/`. The web service is the one exception: run it
as `cd src && python3 -m uvicorn webapp.app:app` so uvicorn can find the `webapp` package.

## Architecture

A **daily-return-stitching engine** (deliberately distinct from a trade/round-trip
backtester): the portfolio earns the primary's return on days its hold rule is on, and a
candidate's (or cash's) return on days it is off; all metrics are computed on the single
stitched equity curve. The entire tool lives in `src/rebound.py` as a pipeline of pure
functions; `src/data_loader.py` is the only other module (a read-only parquet cache reader).

Data flow (see `scan()` for orchestration, `main()` for the CLI):

```
load_ohlc(primary) ─▶ primary_held(df, rule)  → daily bool "exposed to primary" mask
load_ohlc(candidates) ─▶ for each candidate:
      stitch(held, primary_ret, cand_ret, mode)  → naked + filtered portfolio returns
      performance_metrics + antifragility_metrics → one flat row dict (…_n / …_f keys)
   ─▶ rank_candidates(rows, sort_key)  → table (stdout) + recommendation + equity/DD PNG
```

### Invariants you must preserve

- **No look-ahead.** Every signal/health mask ends in `.shift(1, fill_value=False)` so the
  decision for day `t` uses only data through `close[t-1]`. `test_no_lookahead_future_invariance`
  guards this by mutating the last bar and asserting no past value changes — never weaken it.
- **Metrics are stored as fractions** internally (`max_dd = 0.416`), formatted to percent only
  for display. CLI percentages (`--max-dd 25`) are divided by 100 before comparison.
- **Ranking is on naked-mode metrics** (the `_n` row keys); filtered metrics (`_f`) are shown
  alongside for comparison but never drive the sort.
- **Default sort is `antifragile` = `cagr_n × (1 − corr_off)`** — annualized naked return scaled
  by diversification, so a true diversifier (low/negative correlation with the primary on its
  off days) outranks a higher-CAGR name that merely co-moves. `corr_off`/`park_return` are the
  off-period antifragility reads.
- **Two parking modes, two rules, both fixed by design:** modes `naked` (park whenever flat) /
  `filtered` (park only when candidate `close > MA50`, else cash); rules `price_above_ma` /
  `ma_cross`. The spec intentionally excludes jojo oscillator signals as a primary rule.
- **matplotlib is imported lazily** inside `plot_curves` with the `Agg` backend, so the module
  imports (and tests run) without a display or even without matplotlib for non-plot use.

### Web service (`src/webapp/`)

A FastAPI layer over the engine (no engine behavior change). `app.py` (lifespan
warmup + routes + SSE + static mount), `engine_service.py` (warm in-memory frame
store + `run_scan` over the engine's pure functions, bypassing `scan()`'s disk
re-read), `jobs.py` (single-thread background scan registry), `cache_sync.py`
(boto3 S3 → disk), `idle.py` (opt-in idle auto-stop), `static/` (ECharts SPA).
Run as `uvicorn webapp.app:app` from `src/`, **single worker** (the warm cache +
job registry are process-local). Engine reuse must exclude the primary from
candidates, load the primary on demand, and build rule-specific params (the
`curve_series`/`evaluate_iter` helpers are additive in `rebound.py`). Candidate
filters (all best-effort, no-op if their warmup fetch fails): `exclude_etf` via
`load_etf_set()` (NASDAQ/NYSE ETF=Y col — nasdaqlisted 6, otherlisted 4); `sp500_only`
via `load_sp500_set()` (datahub CSV); and `require_full_history` (default **on**) which
requires a backup to span ~90% of the primary's window — without it the antifragile
sort surfaces short-history / micro-cap names (the ILLR survivorship trap). The SPA
falls back to polling `/result` if the progress SSE drops mid-scan (a long scan can
outlast the ALB idle timeout), so a dropped stream never loses a finished scan. Tests:
`src/test_webapp.py` (hand-rolled, no cache). Dev/offline: `FEIYANG_DEV_FIXTURE=1`.

**Python ≥ 3.10 required** for the web layer (FastAPI 0.138.1 / uvicorn 0.49.0 /
sse-starlette 3.4.5). The engine `test_rebound.py` still runs on older Python.
`requirements-dev.txt` (adds `httpx2`) is needed only to run `test_webapp.py`.

### Cache contract (`data_loader.py`)

Reads `data/ohlc/` (gitignored), one parquet per ticker: `stocks/` for equities, `extras/`
for tickers containing `=` (futures, e.g. `GC=F`). Frames are `DatetimeIndex` × `[open, high,
low, close]`. The cache is **produced** by the upstream `jojo_quant` project and synced from S3;
this repo is a read-only consumer.

## Gotchas

- **`docs/` references modules that do not exist here.** The design/plan specs mention
  `src/download_ohlc.py --init`, `README.zh.md`, `backtest.py`, and `indicators.py` — all
  inherited from `jojo_quant`. In *this* repo there is no `download_ohlc.py` (populate the cache
  with `bash scripts/pull_cache.sh`) and no `README.zh.md`. Don't follow those doc instructions
  literally; trust the actual files.
- Tests need **no cache** (synthetic in-memory frames via `make_df`); only real `rebound.py`
  runs need `data/ohlc/` populated.
- When changing source behavior, update `README.md` in the same change (the repo's standing
  doc-sync rule). There is no `README.zh.md` to mirror despite what `docs/` implies.
- **The web service must run single-worker.** The warm frame cache (`engine_service.py`
  `_STORE`) and the job registry (`jobs.py` `_JOBS`) are process-local dicts; multiple
  workers would each hold a separate copy, breaking job lookup and doubling RAM usage.
  Never add `--workers N` (N > 1) to the uvicorn invocation.
- **`test_webapp.py` needs `requirements-dev.txt`** (`httpx2`, the FastAPI TestClient
  dependency). `requirements.txt` alone is sufficient for production but not for tests.
  Install both: `pip install -r requirements.txt -r requirements-dev.txt`.
