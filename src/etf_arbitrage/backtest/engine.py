"""Position simulation and convergence diagnostics on the ETF premium spread."""

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Iterable, List, Optional, Tuple

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
    gross_return: float = 0.0
    total_cost: float = 0.0
    holding_seconds: float = 0.0
    max_adverse_excursion: float = 0.0
    max_favorable_excursion: float = 0.0


@dataclass(frozen=True)
class ConvergenceMetrics:
    """Diagnostics describing whether and how quickly premium gaps converge."""

    trade_count: int = 0
    converged_trade_count: int = 0
    convergence_rate: float = 0.0
    median_convergence_seconds: float = 0.0
    p90_convergence_seconds: float = 0.0
    average_mae_bps: float = 0.0
    worst_mae_bps: float = 0.0
    average_mfe_bps: float = 0.0
    median_gross_capture_bps: float = 0.0
    horizon_rates: Tuple[Tuple[int, float], ...] = ()

    def rate_within(self, seconds: int) -> float:
        return dict(self.horizon_rates).get(int(seconds), 0.0)


@dataclass(frozen=True)
class CostSensitivityPoint:
    """Net spread capture under one assumed round-trip cost."""

    round_trip_cost_bps: float
    trade_count: int
    convergence_rate: float
    profitable_trade_rate: float
    total_net_capture_bps: float
    median_net_capture_bps: float


@dataclass(frozen=True)
class BacktestResult:
    timeline: pd.DataFrame
    trades: Tuple[Trade, ...]
    performance: PerformanceMetrics
    win_rate: float
    average_holding_periods: float
    convergence: ConvergenceMetrics = field(default_factory=ConvergenceMetrics)


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
        frame.sort_values("timestamp", kind="stable", inplace=True)
        frame.reset_index(drop=True, inplace=True)
        if "risk_blocked" not in frame:
            frame["risk_blocked"] = False

        position = 0
        holding_periods = 0
        entry_time: Optional[datetime] = None
        entry_premium = 0.0
        trade_return = 0.0
        trade_gross_return = 0.0
        max_adverse_excursion = 0.0
        max_favorable_excursion = 0.0
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
                gross_period_return = position * (premium - previous_premium)
                period_return = gross_period_return
                trade_return += gross_period_return
                trade_gross_return += gross_period_return
                if position:
                    excursion = position * (premium - entry_premium)
                    max_adverse_excursion = min(max_adverse_excursion, excursion)
                    max_favorable_excursion = max(max_favorable_excursion, excursion)

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
                    trade_gross_return = 0.0
                    max_adverse_excursion = 0.0
                    max_favorable_excursion = 0.0
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
                            gross_return=trade_gross_return,
                            total_cost=max(0.0, trade_gross_return - trade_return),
                            holding_seconds=_elapsed_seconds(entry_time, timestamp),
                            max_adverse_excursion=max_adverse_excursion,
                            max_favorable_excursion=max_favorable_excursion,
                        )
                    )
                    entry_time = None
                    trade_return = 0.0
                    trade_gross_return = 0.0
                    max_adverse_excursion = 0.0
                    max_favorable_excursion = 0.0
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
                    gross_return=trade_gross_return,
                    total_cost=max(0.0, trade_gross_return - trade_return),
                    holding_seconds=_elapsed_seconds(entry_time, final_timestamp),
                    max_adverse_excursion=max_adverse_excursion,
                    max_favorable_excursion=max_favorable_excursion,
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
            convergence=_calculate_convergence_metrics(trades),
        )

    def cost_sensitivity(
        self,
        observations: pd.DataFrame,
        round_trip_costs_bps: Iterable[float] = (6.0, 15.0, 30.0, 50.0),
    ) -> Tuple[CostSensitivityPoint, ...]:
        """Re-run identical signals under alternative round-trip cost assumptions."""
        points: List[CostSensitivityPoint] = []
        seen: set[float] = set()
        for value in round_trip_costs_bps:
            round_trip_cost = float(value)
            if not np.isfinite(round_trip_cost) or round_trip_cost < 0:
                raise ValueError("round-trip costs must be finite and non-negative")
            if round_trip_cost in seen:
                continue
            seen.add(round_trip_cost)
            scenario_config = replace(
                self.config,
                transaction_cost_bps=round_trip_cost / 2.0,
            )
            result = PremiumBacktester(scenario_config).run(observations)
            net_captures = [trade.net_return * 10_000.0 for trade in result.trades]
            points.append(
                CostSensitivityPoint(
                    round_trip_cost_bps=round_trip_cost,
                    trade_count=len(result.trades),
                    convergence_rate=result.convergence.convergence_rate,
                    profitable_trade_rate=result.win_rate,
                    total_net_capture_bps=float(sum(net_captures)),
                    median_net_capture_bps=(
                        float(np.median(net_captures)) if net_captures else 0.0
                    ),
                )
            )
        return tuple(points)


def _elapsed_seconds(entry_time: Optional[datetime], exit_time: datetime) -> float:
    if entry_time is None:
        return 0.0
    return max(0.0, float((exit_time - entry_time).total_seconds()))


def _calculate_convergence_metrics(trades: List[Trade]) -> ConvergenceMetrics:
    if not trades:
        return ConvergenceMetrics(horizon_rates=((30, 0.0), (60, 0.0), (300, 0.0)))

    converged = [trade for trade in trades if trade.exit_reason == "converged"]
    convergence_seconds = [trade.holding_seconds for trade in converged]
    horizons = (30, 60, 300)
    horizon_rates = tuple(
        (
            seconds,
            sum(trade.holding_seconds <= seconds for trade in converged) / len(trades),
        )
        for seconds in horizons
    )
    mae_bps = [
        max(0.0, -trade.max_adverse_excursion * 10_000.0) for trade in trades
    ]
    mfe_bps = [
        max(0.0, trade.max_favorable_excursion * 10_000.0) for trade in trades
    ]
    gross_capture_bps = [trade.gross_return * 10_000.0 for trade in trades]
    return ConvergenceMetrics(
        trade_count=len(trades),
        converged_trade_count=len(converged),
        convergence_rate=len(converged) / len(trades),
        median_convergence_seconds=(
            float(np.median(convergence_seconds)) if convergence_seconds else 0.0
        ),
        p90_convergence_seconds=(
            float(np.percentile(convergence_seconds, 90))
            if convergence_seconds
            else 0.0
        ),
        average_mae_bps=float(np.mean(mae_bps)),
        worst_mae_bps=float(max(mae_bps)),
        average_mfe_bps=float(np.mean(mfe_bps)),
        median_gross_capture_bps=float(np.median(gross_capture_bps)),
        horizon_rates=horizon_rates,
    )
