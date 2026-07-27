from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from etf_arbitrage.data import SZSEPCFParser, SubstituteFlag
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


def test_simulated_timeline_stops_at_configured_length():
    pcf = SZSEPCFParser().parse(PCF)
    source = SimulatedMarketDataSource(
        pcf,
        SimulationConfig(total_ticks=2),
    )
    source.start()
    source.step()
    source.step()
    assert source.current_tick == 2
    with pytest.raises(StopIteration):
        source.step()
    assert not source.health().running


def test_simulated_feed_can_start_from_latest_trade_price_anchors():
    pcf = SZSEPCFParser().parse(PCF)
    active = [
        item
        for item in pcf.components
        if item.component_share > 0
        and item.substitute_flag != SubstituteFlag.MANDATORY
    ]
    prices = {
        item.stock_code: 10.0 + index / 100.0
        for index, item in enumerate(active)
    }
    physical = sum(
        item.component_share * prices[item.stock_code] for item in active
    )
    mandatory = sum(
        item.creation_cash_substitute
        for item in pcf.components
        if item.substitute_flag == SubstituteFlag.MANDATORY
    )
    expected_iopv = (
        physical + mandatory + pcf.estimate_cash_component
    ) / pcf.creation_redemption_unit
    etf_price = expected_iopv * 1.002
    source = SimulatedMarketDataSource(
        pcf,
        SimulationConfig(
            scenario=SimulationScenario.NORMAL,
            base_volatility=0.0,
        ),
        initial_component_prices=prices,
        initial_etf_price=etf_price,
        price_seed_source="test_redis",
    )

    snapshot = source.step()

    assert snapshot.internal_iopv == pytest.approx(expected_iopv)
    assert snapshot.etf_order_book.last_price == pytest.approx(etf_price)
    assert source.base_premium_bps == pytest.approx(20.0)
    assert source.price_seed_source == "test_redis"
    assert snapshot.component_order_books[active[0].stock_code].last_price == pytest.approx(
        prices[active[0].stock_code]
    )
    assert snapshot.etf_order_book.source == "simulated:test_redis"


def test_simulated_feed_rejects_incomplete_component_price_seed():
    pcf = SZSEPCFParser().parse(PCF)

    with pytest.raises(ValueError, match="initial component prices are missing"):
        SimulatedMarketDataSource(
            pcf,
            SimulationConfig(),
            initial_component_prices={},
            initial_etf_price=3.7,
        )


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
