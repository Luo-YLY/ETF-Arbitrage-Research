"""Results emitted by executable-arbitrage pricing and filtering."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from etf_arbitrage.executable_config import ArbitrageDirection
from etf_arbitrage.market_data.quality import DataQualityReport
from etf_arbitrage.pricing import BasketExecutionResult, DepthSweepResult


@dataclass(frozen=True)
class DirectionEvaluation:
    direction: ArbitrageDirection
    cu_count: int
    gross_profit: float
    estimated_costs: float
    safety_buffer: float
    net_profit: float
    net_profit_bps: float
    executable: bool
    rejection_reasons: Tuple[str, ...]
    basket: BasketExecutionResult
    etf_sweep: DepthSweepResult


@dataclass(frozen=True)
class CapacityResult:
    direction: ArbitrageDirection
    max_executable_cu: int
    bottleneck_symbol: Optional[str]
    bottleneck_direction: Optional[str]
    bottleneck_quantity: float
    marginal_profits: Tuple[float, ...]


@dataclass(frozen=True)
class ArbitrageEvaluation:
    timestamp: object
    etf_code: str
    internal_iopv: float
    official_iopv: Optional[float]
    last_premium: float
    bid_premium: float
    ask_premium: float
    lower_bound: float
    upper_bound: float
    creation: DirectionEvaluation
    redemption: DirectionEvaluation
    quality: DataQualityReport
    creation_capacity: Optional[CapacityResult] = None
    redemption_capacity: Optional[CapacityResult] = None
