"""Fixed-threshold and rolling z-score opportunity signals."""

from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum
from typing import Deque, Dict, Optional

import numpy as np

from etf_arbitrage.monitor.premium_monitor import PremiumObservation
from etf_arbitrage.risk.risk_engine import RiskSnapshot


class SignalType(str, Enum):
    NONE = "none"
    PREMIUM_ARBITRAGE = "premium_arbitrage"
    DISCOUNT_ARBITRAGE = "discount_arbitrage"
    CLOSE = "close"


@dataclass(frozen=True)
class SignalDecision:
    signal: SignalType
    edge: float
    score: float
    allowed: bool
    reason: str


@dataclass(frozen=True)
class FixedThresholdConfig:
    premium_entry: float = 0.005
    discount_entry: float = 0.005


class FixedThresholdSignal:
    def __init__(self, config: FixedThresholdConfig = FixedThresholdConfig()) -> None:
        self.config = config

    def evaluate(
        self,
        observation: PremiumObservation,
        risk: Optional[RiskSnapshot] = None,
    ) -> SignalDecision:
        if not np.isfinite(observation.premium):
            return SignalDecision(SignalType.NONE, 0.0, 0.0, False, "invalid_iopv")
        if observation.premium_at_bid >= self.config.premium_entry:
            signal = SignalType.PREMIUM_ARBITRAGE
            edge = observation.premium_at_bid
        elif observation.discount_at_ask >= self.config.discount_entry:
            signal = SignalType.DISCOUNT_ARBITRAGE
            edge = observation.discount_at_ask
        else:
            return SignalDecision(SignalType.NONE, 0.0, 0.0, True, "below_threshold")
        if risk is not None and risk.blocked:
            return SignalDecision(signal, edge, edge, False, ",".join(risk.blockers))
        return SignalDecision(signal, edge, edge, True, "threshold_crossed")


@dataclass(frozen=True)
class ZScoreConfig:
    window: int = 30
    entry_z: float = 2.0
    exit_z: float = 0.5
    min_observations: int = 15


class ZScoreSignal:
    def __init__(self, config: ZScoreConfig = ZScoreConfig()) -> None:
        if config.min_observations > config.window:
            raise ValueError("min_observations cannot exceed window")
        self.config = config
        self._history: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=config.window)
        )

    def evaluate(
        self,
        observation: PremiumObservation,
        risk: Optional[RiskSnapshot] = None,
    ) -> SignalDecision:
        history = self._history[observation.etf_code]
        if not np.isfinite(observation.premium):
            return SignalDecision(SignalType.NONE, 0.0, 0.0, False, "invalid_iopv")
        history.append(observation.premium)
        if len(history) < self.config.min_observations:
            return SignalDecision(SignalType.NONE, 0.0, 0.0, True, "warming_up")
        values = np.asarray(history, dtype=float)
        standard_deviation = float(values.std(ddof=1))
        if standard_deviation <= 1e-12:
            return SignalDecision(SignalType.NONE, 0.0, 0.0, True, "zero_volatility")
        z_score = (observation.premium - float(values.mean())) / standard_deviation
        if z_score >= self.config.entry_z:
            signal = SignalType.PREMIUM_ARBITRAGE
        elif z_score <= -self.config.entry_z:
            signal = SignalType.DISCOUNT_ARBITRAGE
        elif abs(z_score) <= self.config.exit_z:
            signal = SignalType.CLOSE
        else:
            signal = SignalType.NONE
        allowed = not (risk is not None and risk.blocked)
        reason = "zscore" if allowed else ",".join(risk.blockers)
        return SignalDecision(signal, abs(observation.premium), z_score, allowed, reason)
