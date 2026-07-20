"""Performance statistics used by spread backtests."""

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PerformanceMetrics:
    total_return: float
    annualized_return: float
    sharpe: float
    max_drawdown: float
    volatility: float


def calculate_performance(
    returns: Sequence[float], annualization_periods: int
) -> PerformanceMetrics:
    series = pd.Series(returns, dtype=float).fillna(0.0)
    if series.empty:
        return PerformanceMetrics(0.0, 0.0, 0.0, 0.0, 0.0)
    equity = (1.0 + series).cumprod()
    total_return = float(equity.iloc[-1] - 1.0)
    periods = max(len(series), 1)
    annualized_return = (
        float((1.0 + total_return) ** (annualization_periods / periods) - 1.0)
        if total_return > -1.0
        else -1.0
    )
    standard_deviation = float(series.std(ddof=1)) if len(series) > 1 else 0.0
    volatility = standard_deviation * np.sqrt(annualization_periods)
    sharpe = (
        float(series.mean() / standard_deviation * np.sqrt(annualization_periods))
        if standard_deviation > 1e-12
        else 0.0
    )
    drawdown = equity / equity.cummax() - 1.0
    return PerformanceMetrics(
        total_return=total_return,
        annualized_return=annualized_return,
        sharpe=sharpe,
        max_drawdown=float(drawdown.min()),
        volatility=float(volatility),
    )
