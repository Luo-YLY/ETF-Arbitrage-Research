import pytest

from etf_arbitrage.market_data import OrderBookLevel
from etf_arbitrage.pricing import SweepSide, sweep_depth


def test_single_and_multi_level_buy_sweep():
    one = sweep_depth([OrderBookLevel(10.0, 100)], 80, SweepSide.BUY)
    assert one.fully_filled
    assert one.total_value == pytest.approx(800.0)
    assert one.levels_consumed == 1

    multi = sweep_depth(
        [OrderBookLevel(10.0, 50), OrderBookLevel(10.1, 100)],
        100,
        SweepSide.BUY,
    )
    assert multi.fully_filled
    assert multi.total_value == pytest.approx(1_005.0)
    assert multi.average_fill_price == pytest.approx(10.05)
    assert multi.worst_fill_price == pytest.approx(10.1)


def test_sell_uses_highest_bid_first_and_reports_shortage():
    result = sweep_depth(
        [OrderBookLevel(9.9, 30), OrderBookLevel(10.0, 20)],
        100,
        SweepSide.SELL,
    )
    assert not result.fully_filled
    assert result.filled_quantity == 50
    assert result.unfilled_quantity == 50
    assert result.total_value == pytest.approx(497.0)
    assert result.failure_reason == "INSUFFICIENT_DEPTH"


def test_zero_quantity_haircut_and_invalid_level():
    zero = sweep_depth([], 0, SweepSide.BUY)
    assert zero.fully_filled and zero.total_value == 0
    haircut = sweep_depth([OrderBookLevel(10, 100)], 60, SweepSide.BUY, 0.5)
    assert not haircut.fully_filled
    invalid = sweep_depth([OrderBookLevel(-1, 100)], 10, SweepSide.BUY)
    assert invalid.failure_reason == "INVALID_LEVEL"
    with pytest.raises(ValueError):
        sweep_depth([], -1, SweepSide.BUY)
