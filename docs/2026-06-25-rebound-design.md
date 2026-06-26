# Rebound (沸羊羊 / 备胎 finder) — Design Spec

**Date:** 2026-06-25
**Module:** `src/rebound.py`
**Status:** Design approved, pending spec review

## 1. Purpose

Given a primary stock and a binary hold/flat rule (e.g. "hold while close > MA120,
otherwise flat"), find the best **parking asset** ("备胎") to deploy capital into
during the primary's *flat* periods — another stock, future, or ETF — so the
stitched equity curve (primary while in-market + parking asset while out-of-market)
beats simply sitting in cash, with controlled drawdown and a degree of
antifragility (ideally the parking asset rallies precisely when the primary is
out).

This is a different computational paradigm from `backtest.py` (which is
round-trip / trade based). Rebound stitches **daily returns** into a single
portfolio equity curve, so it lives in a new module rather than extending the
trade-based engine.

## 1.1 Prior art (reference models)

Two third-party "mixed" models implement this exact idea (hand-picked backups,
provided by the user as reference — "not necessarily correct"):

- **TSLA「530 开车修车」** — primary `ma5 > ma30`; while flat, hold an equal-weight
  **AZO / ORLY** basket; switch back to TSLA on the re-cross. Reported (no leverage):
  CAGR 65.8%, max drawdown −41.6%, 2010-06-29 → 2026-06-24.
- **NVDA「MA225 混合」** — primary `close > MA225`; while flat, hold an equal-weight
  **TJX / ROST** basket. Reported (no leverage): CAGR 40.9%, max drawdown −51.2%,
  1999 → 2026 (primary is in-market ~91% of the time, so the backup rarely engages).
- **「熊市四君子」(S&P MA225)** — *no primary held*: hold an equal-weight
  **WM / WMT / TSCO / LOW** basket only while the S&P is bearish (`close < MA225`), cash
  while bullish. CAGR 9.2%, max drawdown −35.2%, win rate 90%, 1994 → 2026 (~77% of days
  in cash). This isolates the **parking leg alone** — signal ticker decoupled from the
  held asset, with cash (not the index) held during the on-period. In Rebound v1 the same
  question is answered by the `park_return` column (return earned only on parked days);
  the cash-on-period variant and a `park_return` sort key are v2 (see §13).

Both confirm Rebound's mechanics (close-confirm signal, next-open execution,
MAIN/HEDGE state switching). Two cautions they illustrate: (1) the backups are
hindsight-picked defensive names — exactly the overfitting Rebound's systematic
screen must guard against; (2) even "mixed", their drawdowns stay at −42% / −51%,
so "controlled drawdown" is not actually achieved — Rebound's `--max-dd` cap and
filtered-parking mode aim to do better. Rebound sits *upstream* of these models:
it screens the whole market to *discover* such backups rather than assuming them.

## 2. Inputs (CLI)

```bash
# Rule 1: hold while price > MA(X)
python3 src/rebound.py TICKER --rule price_above_ma --ma 120

# Rule 2: hold while MA(fast) > MA(slow)
python3 src/rebound.py TICKER --rule ma_cross --fast 5 --slow 30
```

### Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `TICKER` | (required) | Primary stock symbol (must exist in the OHLC cache) |
| `--rule` | (required) | `price_above_ma` \| `ma_cross` |
| `--ma N` | 120 | MA window for `price_above_ma` |
| `--fast N` / `--slow N` | 5 / 30 | MA windows for `ma_cross` |
| `--max-dd PCT` | none | Optional cap: drop candidates whose naked combined max drawdown exceeds this %. Default applies **no cap** (see F1 below) |
| `--min-history YRS` | 5 | Drop candidates with less than this many years of common history with the primary (de-overfits short-history names; see F2) |
| `--sort KEY` | `antifragile` | Ranking key: `antifragile` \| `cagr` \| `total_return` \| `calmar` \| `sharpe` |
| `--top N` | 30 | Show only the top N candidates in the table |
| `--plot-mode` | `naked` | Which parking mode to draw on the curve: `naked` \| `filtered` |
| `--top-k N` | 3 | How many top candidates to overlay on the curve |
| `--candidates A,B,C` | — | Restrict candidate pool to these tickers (else full cache) |
| `--cost-bps N` | 0 | One-way cost in basis points charged on each switch day |
| `--out PATH` | `data/plots/rebound_<ticker>_<rule>_<date>.png` | Output path for the curve PNG |
| `--limit N` | 0 | Restrict candidate pool to first N tickers (smoke test) |

> **Ranking key note:** default sort is **`antifragile`** = `cagr_n × (1 − corr_off)`
> — annualized naked return scaled by diversification, so a true diversifier
> (low/negative correlation with the primary during its off days) outranks a
> high-return name that merely co-moves with the primary. Raw `cagr` / `total_return`
> / `calmar` / `sharpe` remain available via `--sort`. (Total return across candidates
> is not directly comparable because histories vary in length — hence CAGR-based
> scoring.) See the F1/F2 revision note below for why these defaults changed.

## 3. Architecture & data flow

```
primary TICKER (data_loader cache)
   └─> primary_held(df, rule, **params) -> pd.Series[bool]   # daily "exposed to primary" mask, lag-correct
candidate pool (data_loader cache, full market ∩ min history, futures included)
   └─> for each candidate c:
        common window = primary.index ∩ c.index (post warm-up)
        naked    : port_ret[t] = held[t] ? primary_ret[t] : c_ret[t]
        filtered : port_ret[t] = held[t] ? primary_ret[t]
                                 : (c_healthy[t] ? c_ret[t] : 0)     # c_healthy = c_close > MA50, lag-correct
        - charge --cost-bps on switch days
        - equity = cumprod(1 + port_ret); compute metrics (naked & filtered)
   └─> drop candidates shorter than --min-history; optionally drop max_dd > --max-dd;
       sort by --sort (default `antifragile` = cagr_n × (1 − corr_off))
   └─> OUTPUT 1: ranking table (stdout)
   └─> OUTPUT 2: equity-curve PNG (matplotlib)
```

### Module responsibilities

- `src/rebound.py` — sole entry point. Reuses `data_loader.load_ohlc` / `list_universe`
  for primary + candidate OHLC. No dependency on `backtest.py` / `indicators.py` — the
  hold rules are pure moving-average comparisons.
- No new shared/library code is required outside `rebound.py` for v1; helpers
  (signal layer, stitching, metrics, table, plot) are functions within it. If any
  helper proves reusable later it can be promoted.

## 4. Primary signal layer (pluggable)

`primary_held(df, rule, **params) -> pd.Series[bool]`, indexed by date, aligned to
`df.index`. Semantics: `held[t] == True` means "the portfolio is exposed to the
primary's close[t-1]→close[t] return on day t". All rules are **lag-correct** — the
decision for day `t` uses only information available at or before `close[t-1]`.

- **`price_above_ma`**: `cond[t] = close[t] > SMA(close, ma)[t]`; `held = cond.shift(1)`.
  Warm-up / NaN → `False` (flat).
- **`ma_cross`**: `cond[t] = SMA(close, fast)[t] > SMA(close, slow)[t]`; `held = cond.shift(1)`.
  Warm-up → `False`.

*Execution model:* signals are decided on `close[t-1]` (the `.shift(1)`) and stitching uses
**close-to-close** daily returns — i.e. a one-day signal lag that approximates the reference
models' "close-confirm, next-open execution". Documented; acceptable for v1.

> jojo strategies were considered as a primary rule and dropped: jojo is an oscillator /
> event signal producing sparse trade intervals, ill-suited to a continuous regime/parking
> framework. Rebound's rules are pure moving-average regimes.

## 5. Parking mechanics

For a candidate `c` with daily returns `c_ret` over the common window:

- `off[t] = not held[t]` (primary flat on day t).
- **Naked parking:** parked whenever off. `port_ret[t] = held[t] ? p_ret[t] : c_ret[t]`.
- **Filtered parking:** `c_healthy[t] = (c_close > SMA(c_close, 50))`, lag-correct via
  `.shift(1)`. `port_ret[t] = held[t] ? p_ret[t] : (off[t] & c_healthy[t] ? c_ret[t] : 0)`.
  When off and the candidate is unhealthy, hold cash (0 return).
- **Costs:** track the active asset each day (`primary` / `candidate` / `cash`).
  On any day the active asset changes, subtract `cost_bps / 1e4` from that day's
  `port_ret`. Switch count is reported.

Both modes are computed for every candidate so the table can show them side by side.

## 6. Metrics (per candidate, per mode)

Computed on the candidate's common evaluation window. `ret` = daily portfolio return,
`equity = cumprod(1 + ret)`, `N` = number of trading days.

| Metric | Formula |
|--------|---------|
| `total_return` | `equity[-1] - 1` |
| `cagr` | `equity[-1] ** (252 / N) - 1` |
| `max_dd` | max peak-to-trough drawdown of `equity`, in % |
| `sharpe` | `mean(ret) / std(ret) * sqrt(252)` (rf = 0) |
| `volatility` | `std(ret) * sqrt(252)` (annualized) |
| `calmar` | `cagr / max_dd` |
| `off_frac` | fraction of days where `off` is True |
| `park_return` | cumulative return earned **only on parked days** (∏(1+c_ret) over parked days − 1) — how the backup itself did while parking |
| `corr_off` | Pearson corr of `c_ret` vs `p_ret` over **off days** — antifragility read; lower / negative is more diversifying |
| `antifragile_score` | `cagr_n × (1 − corr_off)` — the default ranking key; shown as `afscore` in the table |
| `switches` | number of asset-change days |

### Baselines (shown at the top of the table)

1. **Primary Buy&Hold** — always long the primary.
2. **Primary + cash** — primary rule on, cash (0) when off. The "no backup" baseline.
3. **Best backup** — the rank-1 candidate after filtering (highlights the uplift).

Baselines in the summary table use the **primary's full available window**. Per-candidate
metrics use that candidate's **common window**. The plot recomputes baselines on the
plotted window for apples-to-apples comparison (see §8).

## 7. Candidate universe & evaluation window

- Pool: `data_loader.list_universe(min_bars=756)` (~3y) ∩ cache, futures (`=`) included.
  The primary ticker itself is excluded from candidates.
- Per candidate: evaluate over `common = primary.index ∩ candidate.index` after both
  warm-ups. If overlap `< 252` bars, mark **thin-sample** and keep it out of the ranking
  (but it may be listed under a caveat).
- `--candidates` / `--limit` narrow the pool; progress is logged for full-pool runs.
- **Overfitting / survivorship caveat** is printed prominently: with a full-market pool,
  the rank-1 backup is very likely a name that simply trended up over the window. Read the
  ranking alongside `corr_off`, `park_return`, and window length — not in isolation.

## 8. Outputs

No report files are written (this is an interactive tool, not part of the `reports/`
monthly archive). Three deliverables:

1. **Ranking table → stdout.** Baselines block, then top-`--top` candidates. One row per
   candidate with both naked and filtered key metrics, e.g.
   `ticker | off_frac | cagr_n | total_n | maxdd_n | calmar_n | sharpe_n | park_ret_n | corr_off | cagr_f | total_f | maxdd_f | calmar_f`.
   (Rendered with pandas `to_string`.)
2. **Strategy curve → PNG (matplotlib).** Two stacked panels over the plotted window:
   (top) equity/NAV curves — the two baselines + top-`--top-k` candidates' combined curves
   for `--plot-mode`; (bottom) the drawdown curve of the rank-1 combined strategy. Title
   shows primary + rule + window. Saved to `--out` (default under gitignored `data/plots/`).
   Uses the non-interactive `Agg` backend so it works headless.
3. **Current recommendation → stdout.** A single line stating today's state — primary
   **in-market** (hold the primary) or **flat** (park) — and, if flat, the suggested
   rank-1 backup to park in right now. Mirrors the reference models' `t1_recommendation`.
   Computed from the held-mask on the most recent bar.

## 9. Dependencies

- Add **`matplotlib`** to `requirements.txt` (latest stable; exact version + plotting API
  to be confirmed against official docs at implementation time). `matplotlib.use("Agg")`
  for headless PNG output.

## 10. Edge cases

- Primary almost always in-market (few off days) → report low `off_frac`; parking is
  near-irrelevant. Surfaced, not an error.
- Candidate history shorter than the common window → evaluate only on overlap; thin-sample
  flagged and kept out of the ranking.
- Daily returns use the cache's adjusted close (close-to-close).
- MA warm-up NaNs → treated as flat (not held), never enter a position.
- `max_dd == 0` (e.g. degenerate all-up curve) → Calmar guarded against div-by-zero.

## 11. Testing

Assert-based, following the project's existing `test_logic.py` convention (no pytest):

- `primary_held` correctness for both rules on small synthetic series, including the
  `shift(1)` no-look-ahead property.
- Stitching: known primary/candidate return series → known equity for both modes.
- Metric math (CAGR, max_dd, Calmar, Sharpe) on a hand-computed example.
- Filtered parking falls back to cash when the candidate is unhealthy.
- `--max-dd` cap excludes the right candidates.

### Reference-model cross-check (directional validation)

Run Rebound on the two prior-art configs and confirm the hand-picked backups surface
near the top of the single-asset ranking (sanity, not exact reproduction — references
use 2-name baskets + fees + leverage):

- `rebound.py TSLA --rule ma_cross --fast 5 --slow 30` → **AZO** and **ORLY** should rank highly.
- `rebound.py NVDA --rule price_above_ma --ma 225` → **TJX** and **ROST** should rank highly.

If AZO/ORLY/TJX/ROST are absent from the cache, this check is skipped with a note.

## 12. Documentation (deliverable)

Per repo rules, the implementation must update `README.md` (English), `README.zh.md`
(Chinese mirror), and `CLAUDE.md` to document `rebound.py` (沸羊羊 / 备胎 finder), its
flags, the three rules, the parking modes, and the matplotlib dependency.

## 13. Out of scope (v2+)

- **Combo / basket backups** (e.g. equal-weight AZO/ORLY). v1 ranks single assets only;
  the user can hand-combine top names. A `--candidates A,B` basket evaluator is a v2 item.
- **Leverage variants + financing cost** (the reference models' 1.0/1.5/2.0 tiers at a 4%
  financing rate). v1 is unleveraged.
- **Decoupled parking leg** — `--on-asset {primary,cash}` (hold cash, not the primary,
  during on-periods) plus a `park_return` sort key, to reproduce the "熊市四君子" structure
  (signal ticker ≠ held asset). v1 surfaces the same info via the `park_return` column.
- Dynamic / walk-forward rotation among multiple backups (v1 is static per-candidate
  ranking only).
- jojo strategies as a primary rule (oscillator / event signals are ill-suited to a
  continuous regime/parking framework — dropped, not deferred).
- Saving the table as CSV / a committed report.
- Interactive (plotly) charts.
- Trade-segment `win_rate` / `odds` metrics (the references report these; deferred —
  Rebound's core metrics are daily-return based).

## 14. Revisions — real-data validation (F1 / F2)

Validated against the live cache (10,802 tickers, fresh through 2026-06-24). The tool
reproduced the reference models — on a sensible candidate pool, **AZO/ORLY rank top for
TSLA `ma5>ma30`** and **ROST/TJX rank high for NVDA `close>MA225`**, all with markedly
lower `corr_off` than a correlated-tech control set (AAPL/AMZN/META/…), confirming they
are genuine diversifiers rather than co-movers. Two issues surfaced and were fixed:

- **F1 — `--max-dd` cap unachievable for volatile primaries.** The naked combined curve
  inherits the **primary's own** in-market drawdown (TSLA ~74%, NVDA ~90%), which a backup
  cannot remove. A fixed 25% cap therefore emptied the ranking. **Fix:** `--max-dd` now
  defaults to **no cap** (optional opt-in); the caveat line notes the combined DD includes
  the primary's own.
- **F2 — raw-CAGR ranking favored short-history high-flyers / co-movers.** **Fix:** added
  `--min-history` (default 5y, filters on the common-window length) and a new default sort
  key **`antifragile` = `cagr_n × (1 − corr_off)`**, which demotes high-return names that
  co-move with the primary (e.g. AMZN drops below AZO/ORLY for TSLA despite a higher raw
  CAGR). Idiosyncratic long-history high-flyers can still top a full-market scan — the
  caveat plus the `corr_off` / `park_return` / `bars` columns remain the human-judgment guardrails.
