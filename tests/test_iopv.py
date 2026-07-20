from datetime import datetime

import pytest

from etf_arbitrage.data import ComponentWeight, ETFInfo, StockQuote
from etf_arbitrage.valuation import IOPVCalculator, MissingPricePolicy


def info() -> ETFInfo:
    return ETFInfo("159919", "测试ETF", "SZSE", "测试指数", 100.0, 10, 20.0)


def test_iopv_normalizes_weights_and_adds_cash_per_share() -> None:
    weights = [
        ComponentWeight("159919", "000001", 4.0),
        ComponentWeight("159919", "000002", 6.0),
    ]
    quotes = {
        "000001": StockQuote(datetime(2026, 1, 1), "000001", 1.0, 100),
        "000002": StockQuote(datetime(2026, 1, 1), "000002", 2.0, 100),
    }

    result = IOPVCalculator().calculate(info(), weights, quotes)

    assert result.valid
    assert result.equity_value == pytest.approx(1.6)
    assert result.cash_per_share == pytest.approx(0.2)
    assert result.theoretical_price == pytest.approx(1.8)
    assert result.weight_sum == pytest.approx(1.0)


def test_missing_price_policy_is_explicit() -> None:
    weights = [
        ComponentWeight("159919", "000001", 0.4),
        ComponentWeight("159919", "000002", 0.6),
    ]
    quotes = {
        "000001": StockQuote(datetime(2026, 1, 1), "000001", 1.0, 100),
    }

    reweighted = IOPVCalculator(MissingPricePolicy.REWEIGHT).calculate(
        info(), weights, quotes
    )
    strict = IOPVCalculator(MissingPricePolicy.STRICT).calculate(info(), weights, quotes)

    assert reweighted.valid
    assert reweighted.equity_value == pytest.approx(1.0)
    assert reweighted.missing_weight == pytest.approx(0.6)
    assert not strict.valid
    assert strict.quality == "invalid"
