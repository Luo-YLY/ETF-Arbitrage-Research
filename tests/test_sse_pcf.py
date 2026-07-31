import json
from pathlib import Path
import shutil
from uuid import uuid4

import pandas as pd
import pytest

from etf_arbitrage.data import (
    PCFRepository,
    SSEPCFParser,
    SZRedisQuotationClient,
    SZRedisSettings,
    SZSEPCFParser,
    etf_profile,
    extract_etf_code,
    load_pcf_price_seed,
)
from etf_arbitrage.domain import Exchange, InstrumentId, vendor_symbol


def sse_payload():
    return {
        "source": "SSE_PUBLIC_QUERY",
        "header": {
            "result": [
                {
                    "NAV": "￥4.6069",
                    "NET_REDEMPTION_LIMIT": "-",
                    "REDEMPTION_LIMIT_PER_ACCT": "-",
                    "CREATION_REDEMPTION_UNIT": "900000",
                    "FUND_NAME": "华泰柏瑞沪深300交易型开放式指数证券投资基金",
                    "FUND_COMP_NAME": "华泰柏瑞基金管理有限公司",
                    "ETF_TYPE": "8",
                    "CREATION_REDEMPTION": "申购和赎回皆允许",
                    "NET_CREATION_LIMIT": "-",
                    "NAVPERCU": "￥4146198.90",
                    "CREATION_LIMIT": "-",
                    "CREATION_LIMIT_PER_ACCT": "-",
                    "TRADING_DAY": "20260731",
                    "FILE_ID": "1453134",
                    "REDEMPTION_LIMIT": "3600000000",
                    "PRE_TRADING_DAY": "20260730",
                    "PRE_CASH_COMPONENT": "￥93589.90",
                    "ESTIMATED_CASH_COMPONENT": "￥89184.90",
                    "RECORD_NUM": "3",
                    "PUBLISH_IOPV": "是",
                    "TRADE_CODE": "510300",
                    "MAX_CASH_RATIO": "50%",
                    "NET_CREATION_LIMIT_PER_ACCT": "-",
                    "CREATION_REDEMPTION_MECHANISM": "1",
                    "NET_REDEMPTION_LIMIT_PER_ACCT": "-",
                }
            ]
        },
        "components": {
            "result": [
                {
                    "REDEMPTION_DISCOUNT_RATE": "10%",
                    "INSTRUMENT_NAME": "浦发银行",
                    "ETF_VERSION": "XML",
                    "UNDERLYION_SECURITY_ID": "101",
                    "QUANTITY": "1000",
                    "SUBSTITUTION_FLAG": "0",
                    "SUBSTITUTION_CASH_AMOUNT": "10000.000",
                    "ETF_CLASS": "2",
                    "CREATION_PREMIUM_RATE": "10%",
                    "INSTRUMENT_ID": "600000",
                },
                {
                    "REDEMPTION_DISCOUNT_RATE": "10%",
                    "INSTRUMENT_NAME": "平安银行",
                    "ETF_VERSION": "XML",
                    "UNDERLYION_SECURITY_ID": "102",
                    "QUANTITY": "1600",
                    "SUBSTITUTION_FLAG": "1",
                    "SUBSTITUTION_CASH_AMOUNT": "18576.000",
                    "ETF_CLASS": "2",
                    "CREATION_PREMIUM_RATE": "10%",
                    "INSTRUMENT_ID": "000001",
                },
                {
                    "REDEMPTION_DISCOUNT_RATE": "-",
                    "INSTRUMENT_NAME": "现金替代样本",
                    "ETF_VERSION": "TXT",
                    "UNDERLYION_SECURITY_ID": "102",
                    "QUANTITY": "0",
                    "SUBSTITUTION_FLAG": "4",
                    "SUBSTITUTION_CASH_AMOUNT": "1234.500",
                    "ETF_CLASS": "2",
                    "CREATION_PREMIUM_RATE": "-",
                    "INSTRUMENT_ID": "000002",
                },
            ]
        },
    }


def test_parses_official_sse_header_components_and_market_ids() -> None:
    document = SSEPCFParser().parse_payload(sse_payload())

    assert document.etf_id == InstrumentId(Exchange.SSE, "510300")
    assert document.creation_redemption_unit == 900000
    assert document.max_cash_ratio == pytest.approx(0.5)
    assert document.creation_allowed and document.redemption_allowed
    assert [item.instrument_id for item in document.components] == [
        InstrumentId(Exchange.SSE, "600000"),
        InstrumentId(Exchange.SZSE, "000001"),
        InstrumentId(Exchange.SZSE, "000002"),
    ]
    assert document.components[1].premium_ratio == pytest.approx(0.1)
    assert document.components[2].substitute_flag.requires_cash_substitution


def test_repository_stores_sse_pcf_as_json() -> None:
    root = Path("tmp") / "tests" / ("sse-pcf-" + uuid4().hex)
    repository = PCFRepository(root)
    try:
        path = repository.save(
            json.dumps(sse_payload(), ensure_ascii=False).encode("utf-8"),
            "510300",
            "20260731",
        )
        document = repository.validate(path, "510300.SH", "20260731")

        assert path.suffix == ".json"
        assert document.parser_version == "sse-public-json-v1"
    finally:
        shutil.rmtree(root, ignore_errors=True)


class CaptureQuotationClient(SZRedisQuotationClient):
    def __init__(self):
        super().__init__(SZRedisSettings(host="example.invalid"))
        self.requested = []

    def get_security_records(self, codes, trade_date=None):
        self.requested = list(codes)
        return pd.DataFrame(
            {
                "closepx": [4.60, 10.0, 12.0],
            },
            index=["510300.SH", "600000.SH", "000001.SZ"],
        )


def test_price_seed_uses_component_exchange_suffixes() -> None:
    document = SSEPCFParser().parse_payload(sse_payload())
    client = CaptureQuotationClient()

    seed = load_pcf_price_seed(client, document, trade_date="20260731")

    assert client.requested == ["510300.SH", "600000.SH", "000001.SZ"]
    assert seed.component_prices == {"600000": 10.0, "000001": 12.0}


def test_suffix_and_dynamic_etf_search_mapping() -> None:
    assert InstrumentId.parse("510300.SH") == InstrumentId(Exchange.SSE, "510300")
    assert InstrumentId.parse("159919.SZ") == InstrumentId(
        Exchange.SZSE,
        "159919",
    )
    assert vendor_symbol(InstrumentId(Exchange.BSE, "920001")) == "920001.BJ"
    assert extract_etf_code("510300 沪深300ETF华泰柏瑞 [SH]") == "510300"
    assert etf_profile("588000").exchange == Exchange.SSE


def test_szse_cross_market_record_num_counts_local_components() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<PCF xmlns="http://ts.szse.cn/Fund">
  <Version>1.0</Version>
  <SecurityID>159919</SecurityID><SecurityIDSource>102</SecurityIDSource>
  <Symbol>沪深300ETF嘉实</Symbol><FundManagementCompany>嘉实基金</FundManagementCompany>
  <UnderlyingSecurityID>000300</UnderlyingSecurityID>
  <UnderlyingSecurityIDSource>101</UnderlyingSecurityIDSource>
  <CreationRedemptionUnit>900000</CreationRedemptionUnit>
  <EstimateCashComponent>0</EstimateCashComponent><MaxCashRatio>0.5</MaxCashRatio>
  <Publish>Y</Publish><Creation>Y</Creation><Redemption>Y</Redemption>
  <RecordNum>1</RecordNum><TotalRecordNum>2</TotalRecordNum>
  <TradingDay>20260731</TradingDay><PreTradingDay>20260730</PreTradingDay>
  <CashComponent>0</CashComponent><NAVperCU>900000</NAVperCU><NAV>1</NAV>
  <Components>
    <Component>
      <UnderlyingSecurityID>000001</UnderlyingSecurityID>
      <UnderlyingSecurityIDSource>102</UnderlyingSecurityIDSource>
      <UnderlyingSymbol>平安银行</UnderlyingSymbol><ComponentShare>100</ComponentShare>
      <SubstituteFlag>1</SubstituteFlag><PremiumRatio>0.1</PremiumRatio>
    </Component>
    <Component>
      <UnderlyingSecurityID>600000</UnderlyingSecurityID>
      <UnderlyingSecurityIDSource>101</UnderlyingSecurityIDSource>
      <UnderlyingSymbol>浦发银行</UnderlyingSymbol><ComponentShare>100</ComponentShare>
      <SubstituteFlag>1</SubstituteFlag><PremiumRatio>0.1</PremiumRatio>
    </Component>
  </Components>
</PCF>"""
    root = Path("tmp") / "tests" / ("szse-cross-market-" + uuid4().hex)
    path = root / "pcf_159919_20260731.xml"
    try:
        root.mkdir(parents=True)
        path.write_text(xml, encoding="utf-8")
        document = SZSEPCFParser().parse(path)

        assert document.record_num == 1
        assert document.total_record_num == 2
        assert {item.instrument_id.exchange for item in document.components} == {
            Exchange.SSE,
            Exchange.SZSE,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)
