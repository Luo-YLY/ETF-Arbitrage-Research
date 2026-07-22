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
    assert result.performance.total_return > 0
    assert result.win_rate == 1.0


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
