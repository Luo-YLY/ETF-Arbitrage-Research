"""Historical replay and spread-position simulation."""

from .engine import BacktestResult, PremiumBacktester, Trade
from .replay_engine import ResearchReplay

__all__ = ["BacktestResult", "PremiumBacktester", "ResearchReplay", "Trade"]
