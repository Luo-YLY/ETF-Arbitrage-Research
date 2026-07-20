import json

import pytest

from etf_arbitrage.data import (
    ComponentWeight,
    ETFInfo,
    QuotationSchemaError,
    SZRedisDataFeed,
    SZRedisQuotationClient,
    SZRedisSettings,
)


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
        "159915": {"code": "159915", "closepx": 1.205, **common},
        "000001": {"code": "000001", "closepx": 1.0, **common},
        "300750": {"code": "300750", "closepx": 1.4, **common},
    }


def client():
    return SZRedisQuotationClient(
        SZRedisSettings(host="example.invalid"),
        redis_client=FakeRedis(records()),
    )


def test_reads_single_security_and_market_snapshot() -> None:
    quotation_client = client()

    record = quotation_client.get_security_record("159915", "20260720")
    frame = quotation_client.get_quotation_snapshot("20260720")

    assert record is not None
    assert record["closepx"] == pytest.approx(1.205)
    assert list(frame.index) == ["159915", "000001", "300750"]


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
    del broken_records["159915"]["bidpx1"]
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
