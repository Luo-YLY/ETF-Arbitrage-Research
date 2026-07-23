from datetime import timedelta
from pathlib import Path

import pandas as pd

from etf_arbitrage.data import SZSEPCFParser
from etf_arbitrage.executable_config import RedisConfig, SimulationConfig, SimulationScenario
from etf_arbitrage.market_data import (
    DynamicMarketDataLoader,
    FileReplayMarketDataSource,
    RedisMarketDataSource,
    SimulatedMarketDataSource,
)


PCF = Path("data/pcf/20260722/pcf_159915_20260722.xml")


def test_simulated_feed_is_reproducible_and_pcf_consistent():
    pcf = SZSEPCFParser().parse(PCF)
    config = SimulationConfig(random_seed=7, scenario=SimulationScenario.PREMIUM_SHOCK)
    left = SimulatedMarketDataSource(pcf, config).step()
    right = SimulatedMarketDataSource(pcf, config).step()
    assert left.internal_iopv == right.internal_iopv
    assert left.etf_order_book.bids == right.etf_order_book.bids
    assert len(left.component_order_books) > 90
    assert left.etf_order_book.best_bid > left.internal_iopv


def test_file_replay_never_returns_future_snapshot():
    frame = pd.DataFrame(
        [
            {
                "timestamp": "2026-07-22T01:30:00Z",
                "symbol": "159915",
                "is_etf": True,
                "last_price": 1.0,
                "bid1_price": 0.999,
                "bid1_quantity": 1000,
                "ask1_price": 1.001,
                "ask1_quantity": 1000,
            },
            {
                "timestamp": "2026-07-22T01:30:01Z",
                "symbol": "159915",
                "is_etf": True,
                "last_price": 1.1,
                "bid1_price": 1.099,
                "bid1_quantity": 1000,
                "ask1_price": 1.101,
                "ask1_quantity": 1000,
            },
        ]
    )
    snapshots, _ = DynamicMarketDataLoader().from_frame(frame)
    source = FileReplayMarketDataSource(snapshots)
    decision_time = snapshots[0].snapshot_timestamp + timedelta(milliseconds=500)
    assert source.get_at_or_before(decision_time).etf_order_book.last_price == 1.0
    assert source.snapshot_at_or_after(decision_time).etf_order_book.last_price == 1.1


def test_redis_disabled_does_not_import_or_connect():
    source = RedisMarketDataSource(RedisConfig(enabled=False), "159915")
    source.connect()
    assert source.health().status == "DISABLED"
    assert not source.health().connected
