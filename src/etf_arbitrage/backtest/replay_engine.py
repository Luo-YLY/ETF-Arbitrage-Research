"""End-to-end replay from raw quotes to monitored and risk-filtered signals."""

from typing import List, Optional

import pandas as pd

from etf_arbitrage.data.feed import DataFeed
from etf_arbitrage.data.models import ComponentWeight, ETFInfo, MarketSnapshot
from etf_arbitrage.monitor.premium_monitor import PremiumMonitor
from etf_arbitrage.risk.risk_engine import RiskEngine
from etf_arbitrage.signal.arbitrage_signal import FixedThresholdSignal
from etf_arbitrage.valuation.iopv import IOPVCalculator


class ResearchReplay:
    def __init__(
        self,
        feed: DataFeed,
        calculator: Optional[IOPVCalculator] = None,
        monitor: Optional[PremiumMonitor] = None,
        risk_engine: Optional[RiskEngine] = None,
        signal_engine: Optional[FixedThresholdSignal] = None,
    ) -> None:
        self.feed = feed
        self.calculator = calculator or IOPVCalculator()
        self.monitor = monitor or PremiumMonitor()
        self.risk_engine = risk_engine or RiskEngine()
        self.signal_engine = signal_engine or FixedThresholdSignal()

    def run(self, etf_code: str) -> pd.DataFrame:
        info = self.feed.get_etf_info(etf_code)
        weights = self.feed.get_component_weights(etf_code)
        rows: List[dict] = []
        for snapshot in self.feed.snapshots(etf_code):
            rows.append(self.process_snapshot(snapshot, info, weights))
        return pd.DataFrame(rows)

    def process_snapshot(
        self,
        snapshot: MarketSnapshot,
        info: ETFInfo,
        weights: List[ComponentWeight],
    ) -> dict:
        valuation = self.calculator.calculate(info, weights, snapshot.stock_quotes)
        observation = self.monitor.observe(snapshot.etf_quote, valuation)
        risk = self.risk_engine.evaluate(snapshot.etf_quote, weights, snapshot.stock_quotes)
        signal = self.signal_engine.evaluate(observation, risk)
        indicative_blockers = tuple(
            blocker for blocker in risk.blockers if blocker != "missing_bid_ask"
        )
        return {
            "timestamp": snapshot.timestamp,
            "ETF_code": info.etf_code,
            "etf_price": observation.etf_price,
            "bid_price": snapshot.etf_quote.bid_price,
            "ask_price": snapshot.etf_quote.ask_price,
            "has_executable_quote": snapshot.etf_quote.has_executable_quote,
            "iopv": observation.iopv,
            "premium": observation.premium,
            "premium_at_bid": observation.premium_at_bid,
            "discount_at_ask": observation.discount_at_ask,
            "monitor_status": observation.status,
            "deviation_duration_seconds": observation.deviation_duration_seconds,
            "last_recovery_seconds": observation.last_recovery_seconds,
            "valuation_quality": observation.valuation_quality,
            "missing_weight": valuation.missing_weight,
            "suspension_ratio": risk.suspension_ratio,
            "limit_up_ratio": risk.limit_up_ratio,
            "limit_down_ratio": risk.limit_down_ratio,
            "low_liquidity_ratio": risk.low_liquidity_ratio,
            "etf_spread_bps": risk.etf_spread_bps,
            "etf_amount": risk.etf_amount,
            "risk_score": risk.score,
            "risk_level": risk.level.value,
            "risk_blocked": risk.blocked,
            "risk_blockers": ",".join(risk.blockers),
            "indicative_risk_blocked": bool(indicative_blockers),
            "indicative_risk_blockers": ",".join(indicative_blockers),
            "signal": signal.signal.value,
            "signal_allowed": signal.allowed,
            "signal_reason": signal.reason,
            "signal_edge": signal.edge,
        }
