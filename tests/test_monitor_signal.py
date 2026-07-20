from datetime import datetime, timedelta

import pytest

from etf_arbitrage.config import MonitorConfig
from etf_arbitrage.data import ComponentWeight, ETFInfo, ETFQuote, StockQuote
from etf_arbitrage.monitor import PremiumMonitor
from etf_arbitrage.risk import RiskEngine
from etf_arbitrage.signal import (
    FixedThresholdConfig,
    FixedThresholdSignal,
    SignalType,
    ZScoreConfig,
    ZScoreSignal,
)
from etf_arbitrage.valuation import IOPVCalculator


def valuation(timestamp: datetime):
    info = ETFInfo("159919", "测试ETF", "SZSE", "测试指数", 100, 10)
    weights = [ComponentWeight("159919", "000001", 1.0)]
    quotes = {"000001": StockQuote(timestamp, "000001", 1.0, 100, amount=5_000_000)}
    return IOPVCalculator().calculate(info, weights, quotes)


def quote(timestamp: datetime, mid: float) -> ETFQuote:
    return ETFQuote(timestamp, "159919", mid, mid - 0.0001, mid + 0.0001, 100, 10_000_000)


def test_monitor_tracks_excursion_until_recovery_band() -> None:
    start = datetime(2026, 1, 1, 10, 0)
    monitor = PremiumMonitor(MonitorConfig(0.005, 0.001))

    first = monitor.observe(quote(start, 1.006), valuation(start))
    middle = monitor.observe(
        quote(start + timedelta(seconds=30), 1.004),
        valuation(start + timedelta(seconds=30)),
    )
    recovered = monitor.observe(
        quote(start + timedelta(seconds=60), 1.0005),
        valuation(start + timedelta(seconds=60)),
    )

    assert first.status == "deviating"
    assert middle.deviation_duration_seconds == 30
    assert recovered.status == "normal"
    assert recovered.last_recovery_seconds == 60


def test_signal_uses_bid_edge_and_respects_risk_block() -> None:
    timestamp = datetime(2026, 1, 1, 10, 0)
    monitor = PremiumMonitor(MonitorConfig(0.005, 0.001))
    observation = monitor.observe(quote(timestamp, 1.006), valuation(timestamp))
    signal = FixedThresholdSignal(FixedThresholdConfig(0.005, 0.005))

    allowed = signal.evaluate(observation)

    assert allowed.signal == SignalType.PREMIUM_ARBITRAGE
    assert allowed.edge == pytest.approx(0.0059)
    assert allowed.allowed

    weights = [ComponentWeight("159919", "000001", 1.0)]
    suspended_quotes = {
        "000001": StockQuote(
            timestamp, "000001", 1.0, 0, amount=0, is_suspended=True
        )
    }
    risk = RiskEngine().evaluate(quote(timestamp, 1.006), weights, suspended_quotes)
    blocked = signal.evaluate(observation, risk)
    assert blocked.signal == SignalType.PREMIUM_ARBITRAGE
    assert not blocked.allowed
    assert "suspension_or_missing" in blocked.reason


def test_zscore_signal_warms_up_then_detects_extreme_premium() -> None:
    start = datetime(2026, 1, 1, 10, 0)
    monitor = PremiumMonitor(MonitorConfig(0.005, 0.001))
    signal = ZScoreSignal(
        ZScoreConfig(window=4, min_observations=4, entry_z=1.0, exit_z=0.25)
    )
    decisions = []
    for index, premium in enumerate([0.0, 0.0, 0.0, 0.01]):
        timestamp = start + timedelta(minutes=index)
        observation = monitor.observe(
            quote(timestamp, 1.0 + premium), valuation(timestamp)
        )
        decisions.append(signal.evaluate(observation))

    assert decisions[0].reason == "warming_up"
    assert decisions[-1].signal == SignalType.PREMIUM_ARBITRAGE
    assert decisions[-1].score > 1.0
