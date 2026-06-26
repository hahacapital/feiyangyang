"""Read locally-cached OHLC data and metadata.

Returns DataFrames in the exact shape produced by ``screener.download_ohlc``:
``index = DatetimeIndex``, ``columns = [open, high, low, close]``.
That contract lets ``indicators.compute_jojo`` and
``backtest.run_backtest`` consume cache rows with zero changes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "ohlc"
STOCKS_DIR = DATA_DIR / "stocks"
EXTRAS_DIR = DATA_DIR / "extras"
META_PATH = DATA_DIR / "_meta.parquet"
MANIFEST_PATH = DATA_DIR / "_manifest.json"

OHLC_COLS = ["open", "high", "low", "close"]
META_COLS = ["first_date", "last_date", "num_bars", "status", "fail_count"]


def _is_extra(ticker: str) -> bool:
    return "=" in ticker


def ticker_path(ticker: str) -> Path:
    sub = EXTRAS_DIR if _is_extra(ticker) else STOCKS_DIR
    return sub / f"{ticker}.parquet"


def ensure_dirs() -> None:
    STOCKS_DIR.mkdir(parents=True, exist_ok=True)
    EXTRAS_DIR.mkdir(parents=True, exist_ok=True)


def save_ohlc(ticker: str, df: pd.DataFrame) -> None:
    """Write a ticker's OHLC frame to parquet (overwrite)."""
    ensure_dirs()
    out = df[OHLC_COLS].copy()
    out.index = pd.DatetimeIndex(out.index, name="date")
    out.to_parquet(ticker_path(ticker), compression="snappy")


def load_ohlc(ticker: str) -> pd.DataFrame:
    p = ticker_path(ticker)
    if not p.exists():
        raise FileNotFoundError(f"No cached data for {ticker} at {p}")
    df = pd.read_parquet(p)
    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.DatetimeIndex(df.index)
    df.index.name = "date"
    return df


def load_many(tickers: list[str], skip_missing: bool = True) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for t in tickers:
        try:
            out[t] = load_ohlc(t)
        except FileNotFoundError:
            if not skip_missing:
                raise
    return out


def read_meta() -> pd.DataFrame:
    if not META_PATH.exists():
        return pd.DataFrame(columns=META_COLS).rename_axis("ticker")
    df = pd.read_parquet(META_PATH)
    if "ticker" in df.columns:
        df = df.set_index("ticker")
    return df


def write_meta(meta: pd.DataFrame) -> None:
    ensure_dirs()
    out = meta.reset_index() if meta.index.name == "ticker" else meta.copy()
    out.to_parquet(META_PATH, compression="snappy")


def read_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {
            "last_init": None,
            "last_update": None,
            "ticker_count": 0,
            "failed_tickers": [],
            "delisted_tickers": [],
        }
    with open(MANIFEST_PATH) as f:
        return json.load(f)


def write_manifest(d: dict) -> None:
    ensure_dirs()
    with open(MANIFEST_PATH, "w") as f:
        json.dump(d, f, indent=2, default=str)


def list_universe(min_bars: int = 252, exclude_delisted: bool = False) -> list[str]:
    """Return tickers with at least ``min_bars`` cached bars."""
    meta = read_meta()
    if meta.empty:
        return []
    df = meta[meta["num_bars"] >= min_bars]
    if exclude_delisted and "status" in df.columns:
        df = df[df["status"] != "delisted"]
    return sorted(df.index.tolist())
