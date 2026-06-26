"""Rebound (沸羊羊 / 备胎 finder).

Given a primary stock and a hold/flat moving-average rule, find the best
"parking" asset to deploy capital into while the primary is flat. Stitches
daily returns (primary while held, candidate/cash while flat) into one equity
curve, ranks candidates, and outputs a table + an equity/drawdown PNG.

Run: python3 src/rebound.py TICKER --rule price_above_ma --ma 120
"""
from __future__ import annotations

import pandas as pd


# ---------------------------------------------------------------------------
# Signal layer + return/health helpers
# ---------------------------------------------------------------------------

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
    # shift(1) enforces lag-correctness (decide on close[t-1]); fill_value=False
    # keeps bool dtype and avoids a fillna downcast.
    return cond.shift(1, fill_value=False).astype(bool)


def candidate_healthy(df: pd.DataFrame, ma: int = 50) -> pd.Series:
    """Lag-correct 'candidate is healthy' mask: close > SMA(close, ma)."""
    close = df["close"].astype(float)
    cond = close > _sma(close, ma)
    return cond.shift(1, fill_value=False).astype(bool)


# ---------------------------------------------------------------------------
# Daily-return stitching
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Per-candidate evaluation
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Baselines + ranking
# ---------------------------------------------------------------------------

SORT_KEYS = {
    "cagr": "cagr_n",
    "total_return": "total_return_n",
    "calmar": "calmar_n",
    "sharpe": "sharpe_n",
}


def antifragile_score(row: dict) -> float:
    """Return scaled by diversification: cagr_n * (1 - corr_off).

    Lower (or negative) correlation with the primary during its off days
    raises the score, so a true diversifier outranks a high-return name that
    merely co-moves with the primary. This is the default ranking key.
    """
    return row["cagr_n"] * (1.0 - row["corr_off"])


def baseline_metrics(held: pd.Series, primary_ret: pd.Series) -> dict:
    """Reference curves over the primary's full window: always-long buy&hold,
    and the rule on/off with cash (0) when flat — the 'no backup' baseline."""
    buy_hold = performance_metrics(primary_ret)
    primary_cash = performance_metrics(primary_ret.where(held, 0.0))
    return {"buy_hold": buy_hold, "primary_cash": primary_cash}


def rank_candidates(rows: list, *, sort_key: str = "antifragile",
                    max_dd_cap: float | None = None, top: int = 30,
                    min_history_bars: int = 0) -> list:
    """Filter and rank candidates.

    - max_dd_cap (a fraction, or None): drop candidates whose naked combined
      max drawdown exceeds it. None (default) applies no cap — the combined
      curve inherits the primary's own drawdown, so a fixed cap is often
      unachievable for volatile primaries.
    - min_history_bars: drop candidates whose common-window length is shorter
      than this (de-overfits short-history high-flyers).
    - sort_key: 'antifragile' (default, cagr_n * (1 - corr_off)) or one of
      SORT_KEYS, descending.
    """
    kept = list(rows)
    if max_dd_cap is not None:
        kept = [r for r in kept if r["max_dd_n"] <= max_dd_cap]
    if min_history_bars:
        kept = [r for r in kept if r["n_bars"] >= min_history_bars]

    if sort_key == "antifragile":
        keyfn = antifragile_score
    else:
        col = SORT_KEYS[sort_key]
        keyfn = lambda r: r[col]  # noqa: E731
    kept.sort(key=keyfn, reverse=True)
    return kept[:top]


# ---------------------------------------------------------------------------
# Output: table, recommendation, plot
# ---------------------------------------------------------------------------

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
            "afscore": f"{antifragile_score(r) * 100:.1f}",
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

    if os.path.dirname(out_path):
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)
    return out_path


# ---------------------------------------------------------------------------
# Orchestration + CLI
# ---------------------------------------------------------------------------

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
    p.add_argument("--max-dd", type=float, default=None,
                   help="Optional cap: drop candidates whose naked combined max "
                        "drawdown exceeds this %% (default: no cap — the combined "
                        "curve inherits the primary's own drawdown)")
    p.add_argument("--min-history", dest="min_history", type=float, default=5.0,
                   help="Drop candidates with less than this many YEARS of common "
                        "history with the primary (de-overfits short-history names)")
    p.add_argument("--sort", default="antifragile",
                   choices=["antifragile", "cagr", "total_return", "calmar", "sharpe"],
                   help="antifragile = cagr_n*(1-corr_off): rewards diversification")
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

    max_dd_cap = args.max_dd / 100.0 if args.max_dd is not None else None
    min_history_bars = int(args.min_history * 252)
    ranked = rank_candidates(rows, sort_key=args.sort, max_dd_cap=max_dd_cap,
                             top=args.top, min_history_bars=min_history_bars)

    print()
    print("*** Caveat: full-market pool — the rank-1 backup may simply be a name "
          "that trended up over the window. Read alongside corr_off / park_return "
          "/ window length, not in isolation. The naked combined max drawdown "
          "includes the PRIMARY's own drawdown (a backup cannot remove it). ***")
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
