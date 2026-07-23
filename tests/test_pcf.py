import json
from datetime import date, datetime
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET
from uuid import uuid4

import pytest

from etf_arbitrage.backtest import ResearchReplay
from etf_arbitrage.data import (
    ETFInfo,
    StockQuote,
    SubstituteFlag,
    SZRedisDataFeed,
    SZRedisQuotationClient,
    SZRedisSettings,
    SZSEPCFParser,
)
from etf_arbitrage.valuation import PCFIOPVCalculator


PCF_PATH = Path("data/pcf/20260722/pcf_159915_20260722.xml")


def test_parses_real_szse_pcf_sample() -> None:
    pcf = SZSEPCFParser().parse(PCF_PATH)

    assert pcf.etf_code == "159915"
    assert pcf.trading_day == date(2026, 7, 22)
    assert pcf.creation_redemption_unit == 1_000_000
    assert pcf.estimate_cash_component == pytest.approx(2395.07)
    assert pcf.nav == pytest.approx(3.7095)
    assert len(pcf.components) == 100
    assert sum(
        item.substitute_flag == SubstituteFlag.MANDATORY for item in pcf.components
    ) == 2
    assert len([item for item in pcf.components if item.component_share > 0]) == 98


def test_parser_accepts_compact_substitute_fields() -> None:
    namespace = "http://ts.szse.cn/Fund"
    tree = ET.parse(PCF_PATH)
    components = tree.getroot().findall(
        ".//{{{}}}Component".format(namespace)
    )
    allowed = next(
        item
        for item in components
        if item.find("{{{}}}SubstituteFlag".format(namespace)).text == "1"
    )
    mandatory = next(
        item
        for item in components
        if item.find("{{{}}}SubstituteFlag".format(namespace)).text == "2"
    )
    for tag in ("CreationCashSubstitute", "RedemptionCashSubstitute"):
        allowed.remove(allowed.find("{{{}}}{}".format(namespace, tag)))
    for tag in ("ComponentShare", "PremiumRatio"):
        mandatory.remove(mandatory.find("{{{}}}{}".format(namespace, tag)))

    temporary = Path("tmp") / "tests" / uuid4().hex
    temporary.mkdir(parents=True)
    try:
        compact_path = temporary / "compact_pcf.xml"
        tree.write(compact_path, encoding="utf-8", xml_declaration=True)
        pcf = SZSEPCFParser().parse(compact_path)
        allowed_component = next(
            item for item in pcf.components if item.stock_code == "300001"
        )
        mandatory_component = next(
            item
            for item in pcf.components
            if item.substitute_flag == SubstituteFlag.MANDATORY
        )

        assert allowed_component.creation_cash_substitute == 0.0
        assert allowed_component.redemption_cash_substitute == 0.0
        assert mandatory_component.component_share == 0.0
        assert mandatory_component.premium_ratio == 0.0
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def test_pcf_iopv_uses_component_quantities_and_estimated_cash() -> None:
    pcf = SZSEPCFParser().parse(PCF_PATH)
    timestamp = datetime(2026, 7, 22, 10, 0)
    quotes = {
        item.stock_code: StockQuote(timestamp, item.stock_code, 10.0, 100)
        for item in pcf.components
        if item.component_share > 0
    }
    info = pcf.to_etf_info()

    result = PCFIOPVCalculator(pcf).calculate(
        info, pcf.component_weights(), quotes
    )

    expected_basket_value = sum(
        item.component_share * 10.0 for item in pcf.components
    )
    expected = (
        expected_basket_value + pcf.estimate_cash_component
    ) / pcf.creation_redemption_unit
    assert result.valid
    assert result.theoretical_price == pytest.approx(expected)
    assert result.cash_per_share == pytest.approx(0.00239507)
    assert sum(item.contribution for item in result.components) == pytest.approx(
        expected_basket_value / pcf.creation_redemption_unit
    )


def test_pcf_iopv_is_invalid_when_nonzero_component_price_is_missing() -> None:
    pcf = SZSEPCFParser().parse(PCF_PATH)
    timestamp = datetime(2026, 7, 22, 10, 0)
    active = [item for item in pcf.components if item.component_share > 0]
    quotes = {
        item.stock_code: StockQuote(timestamp, item.stock_code, 10.0, 100)
        for item in active[1:]
    }

    result = PCFIOPVCalculator(pcf).calculate(
        pcf.to_etf_info(), pcf.component_weights(), quotes
    )

    assert not result.valid
    assert result.quality == "invalid"
    assert "missing_price" in result.flags
    assert result.missing_weight > 0


def test_pcf_iopv_uses_previous_close_before_first_trade() -> None:
    pcf = SZSEPCFParser().parse(PCF_PATH)
    timestamp = datetime(2026, 7, 22, 9, 20)
    active = [item for item in pcf.components if item.component_share > 0]
    quotes = {
        item.stock_code: StockQuote(timestamp, item.stock_code, 10.0, 100)
        for item in active
    }
    first = active[0]
    quotes[first.stock_code] = StockQuote(
        timestamp, first.stock_code, 0.0, 0, previous_close=9.5
    )

    result = PCFIOPVCalculator(pcf).calculate(
        pcf.to_etf_info(), pcf.component_weights(), quotes
    )

    assert result.valid
    component = next(
        item for item in result.components if item.stock_code == first.stock_code
    )
    assert component.price == pytest.approx(9.5)
    assert component.price_source == "previous_close"


def test_pcf_to_etf_info_uses_creation_unit_and_estimated_cash() -> None:
    pcf = SZSEPCFParser().parse(PCF_PATH)

    info = pcf.to_etf_info()

    assert isinstance(info, ETFInfo)
    assert info.creation_unit == 1_000_000
    assert info.cash_component == pytest.approx(2395.07)


def test_real_pcf_drives_targeted_redis_collection_and_live_iopv() -> None:
    pcf = SZSEPCFParser().parse(PCF_PATH)
    records = {
        "159915.SZ": {
            "code": "159915.SZ",
            "cdate": "20260722",
            "ctime": "100000",
            "closepx": 1.2,
            "amount": 50_000_000,
        }
    }
    records.update(
        {
            "{}.SZ".format(item.stock_code): {
                "code": "{}.SZ".format(item.stock_code),
                "cdate": "20260722",
                "ctime": "100000",
                "closepx": 10.0,
                "preClosepx": 9.9,
                "amount": 10_000_000,
            }
            for item in pcf.components
        }
    )

    class FakeRedis:
        def ping(self):
            return True

        def hmget(self, redis_key, codes):
            assert redis_key == "20260722"
            return [
                json.dumps(records[code], ensure_ascii=False) if code in records else None
                for code in codes
            ]

    client = SZRedisQuotationClient(
        SZRedisSettings(host="example.invalid"), redis_client=FakeRedis()
    )
    feed = SZRedisDataFeed(
        client,
        pcf.to_etf_info(),
        pcf.component_weights(),
        trade_date="20260722",
        redis_code_suffix=".SZ",
        require_bid_ask=False,
    )

    snapshot = list(feed.snapshots("159915"))[0]
    row = ResearchReplay(feed, calculator=PCFIOPVCalculator(pcf)).process_snapshot(
        snapshot, pcf.to_etf_info(), pcf.component_weights()
    )

    assert len(snapshot.stock_quotes) == 100
    assert row["iopv"] > 0
    assert row["valuation_quality"] == "good"
    assert row["signal_reason"] == "missing_bid_ask"
