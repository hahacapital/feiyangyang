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
    exclude_etf: bool = False           # drop ETF candidates from the backups
    require_full_history: bool = True   # backups must span ~90% of the primary window
    sp500_only: bool = False            # restrict candidates to S&P 500 constituents

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
