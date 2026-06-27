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

# ETF tickers, for the optional "exclude ETFs" scan filter. Populated at warmup
# from the NASDAQ/NYSE symbol directories (the ETF=Y column). The two files use
# DIFFERENT ETF column indices: nasdaqlisted col 6, otherlisted col 4.
ETF_TICKERS: set[str] = set()
_ETF_SOURCES = [
    ("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt", 6),
    ("https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt", 4),
]


def load_etf_set() -> int:
    """Best-effort fetch of the ETF ticker set from the NASDAQ/NYSE symbol
    directories. On failure leaves ETF_TICKERS unchanged (exclude-ETF becomes a
    no-op rather than an error). Returns the resulting set size."""
    import urllib.request

    etfs: set[str] = set()
    for url, col in _ETF_SOURCES:
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                text = resp.read().decode("utf-8", "replace")
        except Exception:
            continue
        for line in text.splitlines()[1:]:
            parts = line.split("|")
            if len(parts) > col and parts[col].strip() == "Y":
                sym = parts[0].strip()
                if sym:
                    etfs.add(sym)
    if etfs:
        ETF_TICKERS.clear()
        ETF_TICKERS.update(etfs)
    return len(ETF_TICKERS)


# S&P 500 constituents, for the optional "large-caps only" scan filter (avoids the
# micro-cap / penny-stock names that the full-universe antifragile sort surfaces).
SP500_TICKERS: set[str] = set()
_SP500_SOURCES = [
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv",
    "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/master/data/constituents.csv",
]


def load_sp500_set() -> int:
    """Best-effort fetch of the S&P 500 constituent set (GitHub datahub CSV).
    No-op on failure (the sp500-only filter just won't apply). Returns set size."""
    import csv
    import io
    import urllib.request

    for url in _SP500_SOURCES:
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                rows = list(csv.DictReader(io.StringIO(resp.read().decode("utf-8", "replace"))))
        except Exception:
            continue
        col = next((c for c in (rows[0].keys() if rows else []) if c.lower() in ("symbol", "ticker")), None)
        syms = {r[col].replace(".", "-").strip() for r in rows if col and r.get(col)}
        if syms:
            SP500_TICKERS.clear()
            SP500_TICKERS.update(syms)
            break
    return len(SP500_TICKERS)


class PrimaryNotFound(Exception):
    """The requested primary ticker is not in the OHLC cache."""


class ScanCancelled(Exception):
    """The scan was cancelled via its cancel_event."""


class CandidateNotFound(Exception):
    """The requested candidate ticker is not in the warm universe."""


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
    if getattr(req, "exclude_etf", False) and ETF_TICKERS:
        candidate_frames = {t: f for t, f in candidate_frames.items() if t not in ETF_TICKERS}
    if getattr(req, "sp500_only", False) and SP500_TICKERS:
        candidate_frames = {t: f for t, f in candidate_frames.items() if t in SP500_TICKERS}
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
    min_hist = req.min_history_bars()
    if getattr(req, "require_full_history", False):
        # Backups must span ~all of the primary's window — you can't park in a name
        # that didn't exist yet (the short-history survivorship/look-ahead trap).
        min_hist = max(min_hist, int(0.9 * len(held)))
    ranked = rank_candidates(rows, sort_key=req.sort, max_dd_cap=req.max_dd_cap(),
                             top=req.top, min_history_bars=min_hist)
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


def single_curve(req, ticker: str) -> dict:
    """Combined equity curve for ONE candidate vs the primary, for click-to-view.

    Reuses curve_series with the ticker as the sole pick over its full common
    window with the primary, so the chart can redraw any ranked backup on demand
    without re-running the whole scan."""
    frame = WARM.get(ticker)
    if frame is None:
        raise CandidateNotFound(ticker)
    try:
        primary_df = _load_primary(req.ticker)
    except FileNotFoundError as exc:
        raise PrimaryNotFound(req.ticker) from exc
    held = primary_held(primary_df, req.rule, **req.params())
    primary_ret = daily_returns(primary_df)
    return curve_series(primary_df, held, primary_ret, [{"ticker": ticker}],
                        {ticker: frame}, mode=req.mode, top_k=1)


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
