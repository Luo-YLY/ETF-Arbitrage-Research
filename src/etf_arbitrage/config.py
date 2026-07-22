"""Configuration objects shared by the research engines."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MonitorConfig:
    deviation_threshold: float = 0.005
    recovery_threshold: float = 0.001
    history_size: int = 2_000


@dataclass(frozen=True)
class RiskConfig:
    max_suspension_ratio: float = 0.05
    max_limit_ratio: float = 0.10
    max_etf_spread_bps: float = 20.0
    min_etf_amount: float = 5_000_000.0
    min_stock_amount: float = 2_000_000.0
    max_low_liquidity_ratio: float = 0.15


@dataclass(frozen=True)
class BacktestConfig:
    entry_threshold: float = 0.005
    exit_threshold: float = 0.001
    max_holding_periods: int = 30
    transaction_cost_bps: float = 3.0
    annualization_periods: int = 240 * 252
    execution_mode: str = "indicative"
