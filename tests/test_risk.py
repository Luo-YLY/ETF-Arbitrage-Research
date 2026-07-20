from datetime import datetime

import pytest

from etf_arbitrage.data import ComponentWeight, ETFQuote, LimitStatus, StockQuote
from etf_arbitrage.risk import RiskEngine, RiskLevel


def test_risk_ratios_are_weighted_not_counted() -> None:
    timestamp = datetime(2026, 1, 1, 10, 0)
    weights = [
        ComponentWeight("159919", "A", 0.8),
        ComponentWeight("159919", "B", 0.2),
    ]
    stock_quotes = {
        "A": StockQuote(timestamp, "A", 1.0, 100, amount=5_000_000),
        "B": StockQuote(
            timestamp,
            "B",
            1.0,
            0,
            amount=0,
            is_suspended=True,
            limit_status=LimitStatus.LIMIT_DOWN,
        ),
    }
    etf_quote = ETFQuote(timestamp, "159919", 1.0, 0.9999, 1.0001, 100, 10_000_000)

    result = RiskEngine().evaluate(etf_quote, weights, stock_quotes)

    assert result.suspension_ratio == pytest.approx(0.2)
    assert result.limit_down_ratio == pytest.approx(0.2)
    assert result.low_liquidity_ratio == pytest.approx(0.2)
    assert result.blocked
    assert result.level == RiskLevel.HIGH
