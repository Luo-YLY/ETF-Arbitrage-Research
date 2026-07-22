"""Stateful ETF premium monitor."""

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime
from typing import Deque, Dict, Optional

import pandas as pd

from etf_arbitrage.config import MonitorConfig
from etf_arbitrage.data.models import ETFQuote
from etf_arbitrage.valuation.iopv import IOPVResult


@dataclass(frozen=True)
class PremiumObservation:
    timestamp: datetime
    etf_code: str
    etf_price: float
    iopv: float
    premium: float
    premium_at_bid: float
    discount_at_ask: float
    status: str
    peak_absolute_premium: float
    deviation_duration_seconds: float
    last_recovery_seconds: Optional[float]
    valuation_quality: str


@dataclass
class _MonitorState:
    deviation_started_at: Optional[datetime] = None
    peak_absolute_premium: float = 0.0
    last_recovery_seconds: Optional[float] = None


class PremiumMonitor:
    def __init__(self, config: MonitorConfig = MonitorConfig()) -> None:
        if config.recovery_threshold > config.deviation_threshold:
            raise ValueError("recovery threshold cannot exceed deviation threshold")
        self.config = config
        self._states: Dict[str, _MonitorState] = defaultdict(_MonitorState)
        self._history: Dict[str, Deque[PremiumObservation]] = defaultdict(
            lambda: deque(maxlen=config.history_size)
        )

    def observe(self, quote: ETFQuote, valuation: IOPVResult) -> PremiumObservation:
        state = self._states[quote.etf_code]
        if not valuation.valid or valuation.theoretical_price <= 0:
            observation = PremiumObservation(
                timestamp=quote.timestamp,
                etf_code=quote.etf_code,
                etf_price=quote.mid_price,
                iopv=valuation.theoretical_price,
                premium=float("nan"),
                premium_at_bid=float("nan"),
                discount_at_ask=float("nan"),
                status="invalid_iopv",
                peak_absolute_premium=state.peak_absolute_premium,
                deviation_duration_seconds=0.0,
                last_recovery_seconds=state.last_recovery_seconds,
                valuation_quality=valuation.quality,
            )
            self._history[quote.etf_code].append(observation)
            return observation

        iopv = valuation.theoretical_price
        premium = (quote.mid_price - iopv) / iopv
        premium_at_bid = (
            (quote.bid_price - iopv) / iopv
            if quote.bid_price is not None and quote.bid_price > 0
            else float("nan")
        )
        discount_at_ask = (
            (iopv - quote.ask_price) / iopv
            if quote.ask_price is not None and quote.ask_price > 0
            else float("nan")
        )
        state.peak_absolute_premium = max(state.peak_absolute_premium, abs(premium))

        if state.deviation_started_at is None and abs(premium) >= self.config.deviation_threshold:
            state.deviation_started_at = quote.timestamp
        elif state.deviation_started_at is not None and abs(premium) <= self.config.recovery_threshold:
            state.last_recovery_seconds = max(
                0.0, (quote.timestamp - state.deviation_started_at).total_seconds()
            )
            state.deviation_started_at = None

        duration = (
            max(0.0, (quote.timestamp - state.deviation_started_at).total_seconds())
            if state.deviation_started_at is not None
            else 0.0
        )
        status = "deviating" if state.deviation_started_at is not None else "normal"
        observation = PremiumObservation(
            timestamp=quote.timestamp,
            etf_code=quote.etf_code,
            etf_price=quote.mid_price,
            iopv=iopv,
            premium=premium,
            premium_at_bid=premium_at_bid,
            discount_at_ask=discount_at_ask,
            status=status,
            peak_absolute_premium=state.peak_absolute_premium,
            deviation_duration_seconds=duration,
            last_recovery_seconds=state.last_recovery_seconds,
            valuation_quality=valuation.quality,
        )
        self._history[quote.etf_code].append(observation)
        return observation

    def history_frame(self, etf_code: str) -> pd.DataFrame:
        return pd.DataFrame([item.__dict__ for item in self._history.get(etf_code, [])])
