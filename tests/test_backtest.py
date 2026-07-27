from datetime import datetime, timedelta

import pandas as pd
import pytest

from etf_arbitrage.backtest import PremiumBacktester
from etf_arbitrage.config import BacktestConfig


def test_premium_convergence_generates_positive_spread_return() -> None:
    start = datetime(2026, 1, 1, 10, 0)
    frame = pd.DataFrame(
        {
            "timestamp": [start + timedelta(minutes=index) for index in range(4)],
            "premium": [0.0, 0.006, 0.004, 0.0005],
            "risk_blocked": [False, False, False, False],
        }
    )
    engine = PremiumBacktester(
        BacktestConfig(
            entry_threshold=0.005,
            exit_threshold=0.001,
            transaction_cost_bps=3.0,
        )
    )

    result = engine.run(frame)

    assert len(result.trades) == 1
    assert result.trades[0].direction == "premium"
    assert result.trades[0].exit_reason == "converged"
    assert result.trades[0].net_return == pytest.approx(0.0049)
    assert result.trades[0].gross_return == pytest.approx(0.0055)
    assert result.trades[0].total_cost == pytest.approx(0.0006)
    assert result.trades[0].holding_seconds == pytest.approx(120.0)
    assert result.performance.total_return > 0
    assert result.win_rate == 1.0
    assert result.convergence.convergence_rate == 1.0
    assert result.convergence.rate_within(300) == 1.0


def test_executable_mode_uses_bid_and_ask_edges_for_entry() -> None:
    start = datetime(2026, 1, 1, 10, 0)
    frame = pd.DataFrame(
        {
            "timestamp": [start, start + timedelta(minutes=1)],
            "premium": [0.006, 0.0],
            "premium_at_bid": [0.004, -0.001],
            "discount_at_ask": [-0.008, -0.001],
            "risk_blocked": [False, False],
        }
    )
    engine = PremiumBacktester(
        BacktestConfig(
            entry_threshold=0.005,
            exit_threshold=0.001,
            execution_mode="executable",
        )
    )

    result = engine.run(frame)

    assert len(result.trades) == 0


def test_duplicate_timestamps_keep_source_order() -> None:
    timestamp = datetime(2026, 1, 1, 10, 0)
    frame = pd.DataFrame(
        {
            "timestamp": [timestamp, timestamp, timestamp],
            "premium": [0.006, 0.004, 0.0005],
            "source_order": ["first", "second", "third"],
            "risk_blocked": [False, False, False],
        }
    )

    result = PremiumBacktester(
        BacktestConfig(entry_threshold=0.005, exit_threshold=0.001)
    ).run(frame)

    assert result.timeline["source_order"].tolist() == [
        "first",
        "second",
        "third",
    ]


def test_convergence_metrics_measure_mae_mfe_and_elapsed_time() -> None:
    start = datetime(2026, 1, 1, 10, 0)
    frame = pd.DataFrame(
        {
            "timestamp": [
                start + timedelta(minutes=index) for index in range(5)
            ],
            "premium": [0.0, 0.006, 0.008, 0.003, 0.0005],
            "risk_blocked": [False] * 5,
        }
    )

    result = PremiumBacktester(
        BacktestConfig(
            entry_threshold=0.005,
            exit_threshold=0.001,
            transaction_cost_bps=3.0,
        )
    ).run(frame)

    trade = result.trades[0]
    assert trade.holding_seconds == pytest.approx(180.0)
    assert trade.max_adverse_excursion == pytest.approx(-0.002)
    assert trade.max_favorable_excursion == pytest.approx(0.0055)
    assert result.convergence.average_mae_bps == pytest.approx(20.0)
    assert result.convergence.worst_mae_bps == pytest.approx(20.0)
    assert result.convergence.average_mfe_bps == pytest.approx(55.0)
    assert result.convergence.rate_within(60) == 0.0
    assert result.convergence.rate_within(300) == 1.0


def test_cost_sensitivity_uses_round_trip_cost_assumptions() -> None:
    start = datetime(2026, 1, 1, 10, 0)
    frame = pd.DataFrame(
        {
            "timestamp": [
                start,
                start + timedelta(minutes=1),
                start + timedelta(minutes=2),
            ],
            "premium": [0.006, 0.003, 0.0005],
            "risk_blocked": [False] * 3,
        }
    )
    engine = PremiumBacktester(
        BacktestConfig(entry_threshold=0.005, exit_threshold=0.001)
    )

    points = engine.cost_sensitivity(frame, (6.0, 30.0, 60.0))

    assert [point.round_trip_cost_bps for point in points] == [
        6.0,
        30.0,
        60.0,
    ]
    assert points[0].median_net_capture_bps == pytest.approx(49.0)
    assert points[1].median_net_capture_bps == pytest.approx(25.0)
    assert points[2].median_net_capture_bps == pytest.approx(-5.0)
    assert points[2].profitable_trade_rate == 0.0
