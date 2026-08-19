from datetime import datetime, timedelta
import json
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest

from etf_arbitrage.data import (
    ETFQuote,
    JsonlSnapshotStore,
    MarketSnapshot as RecordedMarketSnapshot,
    PCFComponent,
    PCFDocument,
    SZSEPCFParser,
    StockQuote,
    SubstituteFlag,
)
from etf_arbitrage.executable_config import (
    PaperArbitrageConfig,
    RedisConfig,
    RedisSnapshotFormat,
    SimulationConfig,
    SimulationScenario,
)
from etf_arbitrage.market_data import (
    DataQualityChecker,
    DynamicMarketDataLoader,
    FileReplayMarketDataSource,
    NoNewSnapshotError,
    RecordedHistoryMarketDataSource,
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


def test_recorded_history_replays_each_price_snapshot_with_synthetic_depth():
    pcf = SZSEPCFParser().parse(PCF)
    active = [
        item
        for item in pcf.components
        if item.component_share > 0
        and item.substitute_flag != SubstituteFlag.MANDATORY
    ]
    path = Path("tmp") / "tests" / "{}.jsonl".format(uuid4().hex)
    start = datetime(2026, 7, 22, 9, 30)
    try:
        store = JsonlSnapshotStore(path)
        for tick, etf_price in enumerate((3.60, 3.61)):
            timestamp = start + timedelta(seconds=3 * tick)
            stocks = {
                component.stock_code: StockQuote(
                    timestamp=timestamp,
                    stock_code=component.stock_code,
                    last_price=10.0 + index / 100.0 + tick / 1000.0,
                    previous_close=9.9 + index / 100.0,
                    volume=1_000.0,
                )
                for index, component in enumerate(active)
            }
            store.append(
                RecordedMarketSnapshot(
                    timestamp=timestamp,
                    etf_quote=ETFQuote(
                        timestamp,
                        "159915",
                        etf_price,
                        None,
                        None,
                        1_000,
                        1_000_000,
                    ),
                    stock_quotes=stocks,
                )
            )

        source = RecordedHistoryMarketDataSource(
            path,
            pcf,
            SimulationConfig(
                scenario=SimulationScenario.NORMAL,
                number_of_book_levels=5,
            ),
        )
        source.prime()
        first = source.step()
        second_for_fill = source.snapshot_at_or_after(
            start + timedelta(milliseconds=100)
        )
        second = source.step()

        assert source.total_records == 2
        assert first.snapshot_timestamp == start
        assert first.etf_order_book.last_price == pytest.approx(3.60)
        assert len(first.etf_order_book.bids) == 5
        assert len(first.etf_order_book.asks) == 5
        assert first.etf_order_book.best_bid < 3.60
        assert first.etf_order_book.best_ask > 3.60
        assert len(first.component_order_books) == len(active)
        assert first.source_mode == "LOCAL_HISTORY_SYNTHETIC_BOOK"
        assert second_for_fill is second
        assert second.etf_order_book.last_price == pytest.approx(3.61)
        assert second.snapshot_timestamp == start + timedelta(seconds=3)
        assert source.current_tick == 2
        source.disconnect()
    finally:
        path.unlink(missing_ok=True)


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


def test_file_replay_carries_hkd_cny_inside_each_snapshot():
    timestamp = "2026-08-04T02:00:00Z"
    frame = pd.DataFrame(
        [
            {
                "timestamp": timestamp,
                "symbol": "159920",
                "is_etf": True,
                "exchange": "SZSE",
                "last_price": 1.0,
                "bid1_price": 0.999,
                "bid1_quantity": 1_000,
                "ask1_price": 1.001,
                "ask1_quantity": 1_000,
                "hkd_cny_bid": 0.86,
                "hkd_cny_ask": 0.87,
                "hkd_cny_exchange_timestamp": timestamp,
                "hkd_cny_receive_timestamp": timestamp,
                "hkd_cny_source": "INTRANET_REDIS",
            },
            {
                "timestamp": timestamp,
                "symbol": "00001",
                "is_etf": False,
                "exchange": "HKEX",
                "last_price": 10.05,
                "bid1_price": 10.0,
                "bid1_quantity": 1_000,
                "ask1_price": 10.1,
                "ask1_quantity": 1_000,
            },
        ]
    )

    snapshots, _ = DynamicMarketDataLoader().from_frame(frame)

    assert len(snapshots) == 1
    assert snapshots[0].hkd_cny_quote is not None
    assert snapshots[0].hkd_cny_quote.bid == pytest.approx(0.86)
    assert snapshots[0].hkd_cny_quote.ask == pytest.approx(0.87)
    assert snapshots[0].hkd_cny_quote.source == "INTRANET_REDIS"


def test_redis_disabled_does_not_import_or_connect():
    source = RedisMarketDataSource(RedisConfig(enabled=False), "159915")
    source.connect()
    assert source.health().status == "DISABLED"
    assert not source.health().connected


def test_paper_config_redacts_redis_password_from_exports():
    config = PaperArbitrageConfig(
        redis=RedisConfig(password="do-not-export")
    )

    assert config.to_dict()["redis"]["password"] == "***"
    assert (
        config.to_dict(include_secrets=True)["redis"]["password"]
        == "do-not-export"
    )


class RawHashRedis:
    def __init__(self, records):
        self.records = {
            code: json.dumps(record, ensure_ascii=False)
            for code, record in records.items()
        }

    def ping(self):
        return True

    def hmget(self, key, codes):
        assert key == "20260720"
        return [self.records.get(code) for code in codes]

    def close(self):
        return None


def _raw_book(code: str, market: str, middle: float) -> dict:
    record = {
        "code": code,
        "market": market,
        "status": "E0",
        "cdate": "20260720",
        "ctime": "103000",
        "closepx": middle,
    }
    for level in range(1, 6):
        record["bidPrice{}".format(level)] = middle - level * 0.001
        record["bidVolume{}".format(level)] = 100_000 * level
        record["offerPrice{}".format(level)] = middle + level * 0.001
        record["offerVolume{}".format(level)] = 120_000 * level
    return record


def _small_pcf(exchange: str = "SZSE") -> PCFDocument:
    if exchange == "HKEX":
        etf_code = "159920"
        etf_source = "102"
        listing_exchange = "SZSE"
        component_source = "103"
        component_code = "00001"
    elif exchange == "SSE":
        etf_code = "510300"
        etf_source = "101"
        listing_exchange = "SSE"
        component_source = "101"
        component_code = "600000"
    else:
        etf_code = "159915"
        etf_source = "102"
        listing_exchange = "SZSE"
        component_source = "102"
        component_code = "000001"
    return PCFDocument(
        version="1.0",
        etf_code=etf_code,
        security_id_source=etf_source,
        symbol="test",
        fund_management_company="test",
        underlying_index="test",
        underlying_security_id_source=component_source,
        creation_redemption_unit=1_000,
        estimate_cash_component=0.0,
        max_cash_ratio=1.0,
        publish=True,
        creation_allowed=True,
        redemption_allowed=True,
        record_num=1,
        total_record_num=1,
        trading_day=datetime(2026, 7, 20).date(),
        previous_trading_day=datetime(2026, 7, 17).date(),
        cash_component=0.0,
        nav_per_creation_unit=1_000.0,
        nav=1.0,
        components=(
            PCFComponent(
                stock_code=component_code,
                security_id_source=component_source,
                symbol="component",
                component_share=100.0,
                substitute_flag=SubstituteFlag.ALLOWED,
                premium_ratio=0.0,
                creation_cash_substitute=0.0,
                redemption_cash_substitute=0.0,
                exchange=exchange,
            ),
        ),
        listing_exchange=listing_exchange,
        file_hash="raw-five-level-test",
    )


def test_raw_date_hash_maps_five_levels_records_and_deduplicates():
    pcf = _small_pcf()
    path = Path("tmp") / "tests" / "{}.jsonl".format(uuid4().hex)
    records = {
        "159915.SZ": _raw_book("159915.SZ", "SZ", 1.205),
        "000001.SZ": _raw_book("000001.SZ", "SZ", 10.0),
    }
    source = RedisMarketDataSource(
        RedisConfig(
            enabled=True,
            snapshot_format=RedisSnapshotFormat.DATE_HASH,
            trade_date_key="20260720",
            number_of_book_levels=5,
            recording_path=str(path),
        ),
        "159915",
        pcf,
        redis_client=RawHashRedis(records),
        clock=lambda: datetime(2026, 7, 20, 10, 30, 0, 200_000),
    )
    try:
        snapshot = source.step()

        assert snapshot.source_mode == "REDIS_DATE_HASH"
        assert len(snapshot.etf_order_book.bids) == 5
        assert len(snapshot.etf_order_book.asks) == 5
        assert snapshot.etf_order_book.best_bid == pytest.approx(1.204)
        assert snapshot.etf_order_book.bids[0].quantity == pytest.approx(100_000)
        assert len(snapshot.component_order_books["000001"].asks) == 5
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["etf_order_book"]["bids"][0][0] == pytest.approx(1.204)
        assert payload["etf_order_book"]["bids"][0][1] == pytest.approx(100_000)
        assert "bidPrice5" in payload["raw_records"]["159915.SZ"]
        with pytest.raises(NoNewSnapshotError):
            source.step()
        assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    finally:
        source.disconnect()
        path.unlink(missing_ok=True)


def test_raw_cross_border_hash_can_carry_hkd_cny_quote():
    pcf = _small_pcf("HKEX")
    records = {
        "159920.SZ": _raw_book("159920.SZ", "SZ", 1.5),
        "00001.HK": _raw_book("00001.HK", "HK", 10.0),
        "HKDCNY.FX": _raw_book("HKDCNY.FX", "FX", 0.865),
    }
    source = RedisMarketDataSource(
        RedisConfig(
            enabled=True,
            snapshot_format=RedisSnapshotFormat.DATE_HASH,
            trade_date_key="20260720",
            hkd_cny_code="HKDCNY.FX",
        ),
        "159920",
        pcf,
        redis_client=RawHashRedis(records),
        clock=lambda: datetime(2026, 7, 20, 10, 30, 0, 200_000),
    )

    snapshot = source.step()

    assert snapshot.hkd_cny_quote is not None
    assert snapshot.hkd_cny_quote.bid == pytest.approx(0.864)
    assert snapshot.hkd_cny_quote.ask == pytest.approx(0.866)
    assert snapshot.component_order_books["00001"].exchange == "HKEX"


def test_raw_component_without_two_sided_depth_is_counted_as_missing():
    pcf = _small_pcf()
    component = _raw_book("000001.SZ", "SZ", 10.0)
    for level in range(1, 6):
        component.pop("offerPrice{}".format(level))
        component.pop("offerVolume{}".format(level))
    source = RedisMarketDataSource(
        RedisConfig(
            enabled=True,
            snapshot_format=RedisSnapshotFormat.DATE_HASH,
            trade_date_key="20260720",
        ),
        "159915",
        pcf,
        redis_client=RawHashRedis(
            {
                "159915.SZ": _raw_book("159915.SZ", "SZ", 1.205),
                "000001.SZ": component,
            }
        ),
        clock=lambda: datetime(2026, 7, 20, 10, 30, 0, 200_000),
    )

    report = DataQualityChecker().evaluate(source.step(), pcf)

    assert report.missing_weight == pytest.approx(1.0)
    assert "MISSING_COMPONENT_QUOTES" in report.blockers
    assert "MISSING_COMPONENT_TWO_SIDED_BOOK" in report.blockers


def test_raw_date_hash_maps_sse_etf_and_component_vendor_symbols():
    pcf = _small_pcf("SSE")
    source = RedisMarketDataSource(
        RedisConfig(
            enabled=True,
            snapshot_format=RedisSnapshotFormat.DATE_HASH,
            trade_date_key="20260720",
        ),
        "510300",
        pcf,
        redis_client=RawHashRedis(
            {
                "510300.SH": _raw_book("510300.SH", "SH", 4.2),
                "600000.SH": _raw_book("600000.SH", "SH", 10.0),
            }
        ),
        clock=lambda: datetime(2026, 7, 20, 10, 30, 0, 200_000),
    )

    snapshot = source.step()

    assert snapshot.etf_order_book.exchange == "SSE"
    assert snapshot.etf_order_book.symbol == "510300"
    assert snapshot.component_order_books["600000"].exchange == "SSE"
    assert snapshot.component_order_books["600000"].has_two_sided_book
