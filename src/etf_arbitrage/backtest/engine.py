"""Position simulation on the ETF premium spread."""

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from etf_arbitrage.config import BacktestConfig
from etf_arbitrage.utils.performance import PerformanceMetrics, calculate_performance


@dataclass(frozen=True)
class Trade:
    direction: str
    entry_time: datetime
    exit_time: datetime
    entry_premium: float
    exit_premium: float
    holding_periods: int
    net_return: float
    exit_reason: str


@dataclass(frozen=True)
class BacktestResult:
    timeline: pd.DataFrame
    trades: Tuple[Trade, ...]
    performance: PerformanceMetrics
    win_rate: float
    average_holding_periods: float


class PremiumBacktester:
    """Simulate long/short premium positions until the spread converges."""

    REQUIRED_COLUMNS = {"timestamp", "premium"}

    def __init__(self, config: BacktestConfig = BacktestConfig()) -> None:
        if config.exit_threshold >= config.entry_threshold:
            raise ValueError("exit threshold must be below entry threshold")
        if config.execution_mode not in {"indicative", "executable"}:
            raise ValueError("execution_mode must be indicative or executable")
        self.config = config

    def run(self, observations: pd.DataFrame) -> BacktestResult:
        missing = self.REQUIRED_COLUMNS - set(observations.columns)
        if missing:
            raise ValueError("Missing backtest columns: {}".format(sorted(missing)))
        if self.config.execution_mode == "executable":
            edge_columns = {"premium_at_bid", "discount_at_ask"}
            missing_edges = edge_columns - set(observations.columns)
            if missing_edges:
                raise ValueError(
                    "Executable backtest requires columns: {}".format(
                        sorted(missing_edges)
                    )
                )
        frame = observations.copy()
        frame["timestamp"] = pd.to_datetime(frame["timestamp"])
        frame.sort_values("timestamp", inplace=True)
        frame.reset_index(drop=True, inplace=True)
        if "risk_blocked" not in frame:
            frame["risk_blocked"] = False

        position = 0
        holding_periods = 0
        entry_time: Optional[datetime] = None
        entry_premium = 0.0
        trade_return = 0.0
        trades: List[Trade] = []
        positions: List[int] = []
        actions: List[str] = []
        returns: List[float] = []
        cost_rate = self.config.transaction_cost_bps / 10_000.0
        previous_premium: Optional[float] = None

        for row in frame.itertuples(index=False):
            timestamp = pd.Timestamp(row.timestamp).to_pydatetime()
            premium = float(row.premium)
            risk_blocked = bool(row.risk_blocked)
            period_return = 0.0
            if previous_premium is not None and np.isfinite(premium):
                period_return = position * (premium - previous_premium)
                trade_return += period_return

            new_position = position
            action = "hold" if position else "flat"
            exit_reason: Optional[str] = None
            if not np.isfinite(premium):
                if position:
                    new_position = 0
                    exit_reason = "invalid_data"
            elif position == 0 and not risk_blocked:
                premium_edge = (
                    float(getattr(row, "premium_at_bid"))
                    if self.config.execution_mode == "executable"
                    else premium
                )
                discount_edge = (
                    float(getattr(row, "discount_at_ask"))
                    if self.config.execution_mode == "executable"
                    else -premium
                )
                if np.isfinite(premium_edge) and premium_edge >= self.config.entry_threshold:
                    new_position = -1
                    action = "open_premium"
                elif np.isfinite(discount_edge) and discount_edge >= self.config.entry_threshold:
                    new_position = 1
                    action = "open_discount"
            elif position != 0:
                holding_periods += 1
                if abs(premium) <= self.config.exit_threshold:
                    new_position = 0
                    exit_reason = "converged"
                elif holding_periods >= self.config.max_holding_periods:
                    new_position = 0
                    exit_reason = "max_holding"
                elif risk_blocked:
                    new_position = 0
                    exit_reason = "risk_blocked"

            if new_position != position:
                turnover = abs(new_position - position)
                transaction_cost = turnover * cost_rate
                period_return -= transaction_cost
                trade_return -= transaction_cost
                if position == 0:
                    entry_time = timestamp
                    entry_premium = premium
                    holding_periods = 0
                else:
                    action = "close_{}".format(exit_reason)
                    trades.append(
                        Trade(
                            direction="premium" if position == -1 else "discount",
                            entry_time=entry_time or timestamp,
                            exit_time=timestamp,
                            entry_premium=entry_premium,
                            exit_premium=premium,
                            holding_periods=holding_periods,
                            net_return=trade_return,
                            exit_reason=exit_reason or "closed",
                        )
                    )
                    entry_time = None
                    trade_return = 0.0
                    holding_periods = 0
                position = new_position

            positions.append(position)
            actions.append(action)
            returns.append(period_return)
            previous_premium = premium if np.isfinite(premium) else previous_premium

        if position != 0 and len(frame) > 0:
            final_timestamp = pd.Timestamp(frame.iloc[-1]["timestamp"]).to_pydatetime()
            final_premium = float(frame.iloc[-1]["premium"])
            returns[-1] -= cost_rate
            trade_return -= cost_rate
            actions[-1] = "close_end_of_replay"
            positions[-1] = 0
            trades.append(
                Trade(
                    direction="premium" if position == -1 else "discount",
                    entry_time=entry_time or final_timestamp,
                    exit_time=final_timestamp,
                    entry_premium=entry_premium,
                    exit_premium=final_premium,
                    holding_periods=holding_periods,
                    net_return=trade_return,
                    exit_reason="end_of_replay",
                )
            )

        frame["position"] = positions
        frame["action"] = actions
        frame["strategy_return"] = returns
        frame["equity"] = (1.0 + frame["strategy_return"]).cumprod()
        performance = calculate_performance(
            frame["strategy_return"], self.config.annualization_periods
        )
        closed_returns = [trade.net_return for trade in trades]
        win_rate = (
            sum(item > 0 for item in closed_returns) / len(closed_returns)
            if closed_returns
            else 0.0
        )
        average_holding = (
            float(np.mean([trade.holding_periods for trade in trades])) if trades else 0.0
        )
        return BacktestResult(
            timeline=frame,
            trades=tuple(trades),
            performance=performance,
            win_rate=win_rate,
            average_holding_periods=average_holding,
        )
