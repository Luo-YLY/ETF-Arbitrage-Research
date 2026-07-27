"""Historical replay and spread-position simulation."""

from .engine import (
    BacktestResult,
    ConvergenceMetrics,
    CostSensitivityPoint,
    PremiumBacktester,
    Trade,
)
from .replay_engine import ResearchReplay

__all__ = [
    "BacktestResult",
    "ConvergenceMetrics",
    "CostSensitivityPoint",
    "PremiumBacktester",
    "ResearchReplay",
    "Trade",
]
