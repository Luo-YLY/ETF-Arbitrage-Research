import json
from datetime import date, datetime

import pytest

from etf_arbitrage.data import (
    ComponentWeight,
    ETFInfo,
    PCFComponent,
    PCFDocument,
    QuotationSchemaError,
    SZRedisDataFeed,
    SZRedisQuotationClient,
    SZRedisSettings,
    SZSEPCFParser,
    SubstituteFlag,
    load_pcf_price_seed,
)
from etf_arbitrage.backtest import ResearchReplay


class FakeRedis:
    def __init__(self, records):
        self.records = {
            key: json.dumps(value, ensure_ascii=False) for key, value in records.items()
        }

    def ping(self):
        return True

    def hget(self, redis_key, code):
        assert redis_key == "20260720"
        return self.records.get(code)

    def hkeys(self, redis_key):
        assert redis_key == "20260720"
        return list(self.records)

    def hmget(self, redis_key, codes):
        assert redis_key == "20260720"
        return [self.records.get(code) for code in codes]


def records():
    common = {
        "timestamp": "2026-07-20 10:30:00",
        "bidpx1": 1.204,
        "askpx1": 1.206,
        "volume": 2_000_000,
        "amount": 20_000_000,
    }
    return {
        "159915.SZ": {"code": "159915.SZ", "closepx": 1.205, **common},
        "000001.SZ": {"code": "000001.SZ", "closepx": 1.0, **common},
        "300750.SZ": {"code": "300750.SZ", "closepx": 1.4, **common},
    }


def cross_border_pcf() -> PCFDocument:
    return PCFDocument(
        version="1.0",
        etf_code="159920",
        security_id_source="102",
        symbol="恒生ETF华夏",
        fund_management_company="华夏基金",
        underlying_index="HSI",
        underlying_security_id_source="103",
        creation_redemption_unit=1_000,
        estimate_cash_component=100.0,
        max_cash_ratio=1.0,
        publish=True,
        creation_allowed=True,
        redemption_allowed=True,
        record_num=1,
        total_record_num=1,
        trading_day=date(2026, 7, 20),
        previous_trading_day=date(2026, 7, 17),
        cash_component=90.0,
        nav_per_creation_unit=1_000.0,
        nav=1.0,
        components=(
            PCFComponent(
                stock_code="00001",
                security_id_source="103",
                symbol="长和",
                component_share=100.0,
                substitute_flag=SubstituteFlag.ALLOWED,
                premium_ratio=0.0,
                creation_cash_substitute=0.0,
                redemption_cash_substitute=0.0,
                exchange="HKEX",
            ),
        ),
        listing_exchange="SZSE",
        file_hash="test-cross-border-pcf",
    )


def client():
    return SZRedisQuotationClient(
        SZRedisSettings(host="example.invalid"),
        redis_client=FakeRedis(records()),
    )


def test_reads_single_security_and_market_snapshot() -> None:
    quotation_client = client()

    record = quotation_client.get_security_record("159915.SZ", "20260720")
    frame = quotation_client.get_quotation_snapshot("20260720")

    assert record is not None
    assert record["closepx"] == pytest.approx(1.205)
    assert list(frame.index) == ["159915.SZ", "000001.SZ", "300750.SZ"]


def test_maps_redis_snapshot_to_common_data_feed() -> None:
    info = ETFInfo(
        etf_code="159915",
        name="创业板ETF",
        exchange="SZSE",
        tracking_index="创业板指",
        shares=1_000_000_000,
        creation_unit=1_000_000,
    )
    weights = [
        ComponentWeight("159915", "000001", 0.5),
        ComponentWeight("159915", "300750", 0.5),
    ]
    feed = SZRedisDataFeed(client(), info, weights, trade_date="20260720")

    snapshots = list(feed.snapshots("159915"))

    assert len(snapshots) == 1
    assert snapshots[0].etf_quote.last_price == pytest.approx(1.205)
    assert snapshots[0].etf_quote.bid_price == pytest.approx(1.204)
    assert snapshots[0].stock_quotes["300750"].last_price == pytest.approx(1.4)


def test_rejects_snapshot_without_executable_etf_quote_fields() -> None:
    broken_records = records()
    del broken_records["159915.SZ"]["bidpx1"]
    quotation_client = SZRedisQuotationClient(
        SZRedisSettings(host="example.invalid"),
        redis_client=FakeRedis(broken_records),
    )
    info = ETFInfo("159915", "创业板ETF", "SZSE", "创业板指", 1, 1)
    feed = SZRedisDataFeed(
        quotation_client,
        info,
        [ComponentWeight("159915", "000001", 1.0)],
        trade_date="20260720",
    )

    with pytest.raises(QuotationSchemaError, match="bidpx1"):
        list(feed.snapshots("159915"))


def test_maps_real_last_price_fields_in_indicative_mode() -> None:
    raw = {
        "159915.SZ": {
            "code": "159915.SZ",
            "cdate": "20260721",
            "ctime": "151324",
            "closepx": 3.709,
            "amount": 4_276_929_334,
        },
        "000001.SZ": {
            "code": "000001.SZ",
            "cdate": "20260721",
            "ctime": "151323",
            "closepx": 1.0,
            "preClosepx": 0.99,
            "amount": 10_000_000,
        },
    }
    quotation_client = SZRedisQuotationClient(
        SZRedisSettings(host="example.invalid"), redis_client=FakeRedis(raw)
    )
    info = ETFInfo("159915", "创业板ETF", "SZSE", "创业板指", 1, 1)
    weights = [ComponentWeight("159915", "000001", 1.0)]
    feed = SZRedisDataFeed(
        quotation_client,
        info,
        weights,
        trade_date="20260720",
        require_bid_ask=False,
    )

    snapshot = list(feed.snapshots("159915"))[0]
    row = ResearchReplay(feed).process_snapshot(snapshot, info, weights)

    assert snapshot.timestamp == datetime(2026, 7, 21, 15, 13, 24)
    assert snapshot.etf_quote.bid_price is None
    assert snapshot.etf_quote.mid_price == pytest.approx(3.709)
    assert snapshot.stock_quotes["000001"].previous_close == pytest.approx(0.99)
    assert not snapshot.etf_quote.has_executable_quote
    assert row["premium"] == pytest.approx(2.709)
    assert row["signal_reason"] == "missing_bid_ask"
    assert "missing_bid_ask" in row["risk_blockers"]
    assert not row["indicative_risk_blocked"]


def test_loads_complete_pcf_latest_price_seed() -> None:
    pcf = SZSEPCFParser().parse(
        "data/pcf/20260722/pcf_159915_20260722.xml"
    )
    active = [
        item
        for item in pcf.components
        if item.component_share > 0
        and item.substitute_flag != SubstituteFlag.MANDATORY
    ]
    raw = {
        "159915.SZ": {"code": "159915.SZ", "closepx": 3.709},
        **{
            item.stock_code + ".SZ": {
                "code": item.stock_code + ".SZ",
                "closepx": 10.0 + index / 100.0,
            }
            for index, item in enumerate(active)
        },
    }
    quotation_client = SZRedisQuotationClient(
        SZRedisSettings(host="example.invalid"),
        redis_client=FakeRedis(raw),
    )

    seed = load_pcf_price_seed(
        quotation_client,
        pcf,
        trade_date="20260720",
    )

    assert seed.etf_price == pytest.approx(3.709)
    assert seed.component_count == len(active)
    assert set(seed.component_prices) == {
        item.stock_code for item in active
    }


def test_price_seed_rejects_missing_physical_component() -> None:
    pcf = SZSEPCFParser().parse(
        "data/pcf/20260722/pcf_159915_20260722.xml"
    )
    active = [
        item
        for item in pcf.components
        if item.component_share > 0
        and item.substitute_flag != SubstituteFlag.MANDATORY
    ]
    raw = {
        "159915.SZ": {"code": "159915.SZ", "closepx": 3.709},
        **{
            item.stock_code + ".SZ": {
                "code": item.stock_code + ".SZ",
                "closepx": 10.0,
            }
            for item in active[1:]
        },
    }
    quotation_client = SZRedisQuotationClient(
        SZRedisSettings(host="example.invalid"),
        redis_client=FakeRedis(raw),
    )

    with pytest.raises(QuotationSchemaError, match="缺少PCF实物成分股"):
        load_pcf_price_seed(
            quotation_client,
            pcf,
            trade_date="20260720",
        )


def test_cross_border_price_seed_optionally_reads_two_sided_hkd_cny() -> None:
    pcf = cross_border_pcf()
    active = [
        item
        for item in pcf.components
        if item.component_share > 0
        and item.substitute_flag != SubstituteFlag.MANDATORY
    ]
    raw = {
        "159920.SZ": {"code": "159920.SZ", "closepx": 1.50},
        **{
            item.stock_code + ".HK": {
                "code": item.stock_code + ".HK",
                "closepx": 10.0,
            }
            for item in active
        },
        "HKDCNY.FX": {
            "code": "HKDCNY.FX",
            "closepx": 0.865,
            "bidpx1": 0.86,
            "askpx1": 0.87,
        },
    }
    quotation_client = SZRedisQuotationClient(
        SZRedisSettings(host="example.invalid"),
        redis_client=FakeRedis(raw),
    )

    seed = load_pcf_price_seed(
        quotation_client,
        pcf,
        trade_date="20260720",
        hkd_cny_code="HKDCNY.FX",
    )

    assert seed.component_count == len(active)
    assert seed.hkd_cny_bid == pytest.approx(0.86)
    assert seed.hkd_cny_ask == pytest.approx(0.87)
    assert seed.hkd_cny_code == "HKDCNY.FX"

    seed_without_fx = load_pcf_price_seed(
        quotation_client,
        pcf,
        trade_date="20260720",
    )
    assert seed_without_fx.hkd_cny_bid is None
    assert seed_without_fx.hkd_cny_ask is None
    assert seed_without_fx.hkd_cny_code is None
