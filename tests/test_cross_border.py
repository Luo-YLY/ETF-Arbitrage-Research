from datetime import date, datetime
from pathlib import Path
import shutil
from uuid import uuid4

import pytest

from etf_arbitrage.data import PCFComponent, PCFDocument, SubstituteFlag
from etf_arbitrage.dashboard_panel import CrossBorderPanelDashboard
from etf_arbitrage.dashboard_panel.executable import _cross_border_proxy_pcf
from etf_arbitrage.market_data import (
    CrossBorderSnapshotInterface,
    CrossBorderSnapshotSourceMode,
    DataQualityStatus,
    HKDCNYQuote,
    MarketSnapshot,
    OrderBook,
    OrderBookLevel,
    PendingCrossBorderMarketDataSource,
    calculate_cross_border_indicative_metrics,
    cross_border_hk_components,
    virtual_subscription_cash_component,
)


def _sample_pcf() -> PCFDocument:
    virtual_cash = PCFComponent(
        stock_code="159900",
        security_id_source="102",
        symbol="申赎现金",
        component_share=0.0,
        substitute_flag=SubstituteFlag.MANDATORY,
        premium_ratio=0.0,
        creation_cash_substitute=1_200.0,
        redemption_cash_substitute=0.0,
        exchange="SZSE",
    )
    hk_component = PCFComponent(
        stock_code="00001",
        security_id_source="103",
        symbol="长和",
        component_share=100.0,
        substitute_flag=SubstituteFlag.ALLOWED,
        premium_ratio=0.1,
        creation_cash_substitute=0.0,
        redemption_cash_substitute=0.0,
        exchange="HKEX",
    )
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
        total_record_num=2,
        trading_day=date(2026, 8, 4),
        previous_trading_day=date(2026, 8, 3),
        cash_component=90.0,
        nav_per_creation_unit=1_000.0,
        nav=1.0,
        components=(virtual_cash, hk_component),
        listing_exchange="SZSE",
        file_hash="sample-cross-border-pcf",
    )


def _book(symbol: str, exchange: str, bid: float, ask: float) -> OrderBook:
    timestamp = datetime(2026, 8, 4, 10, 0)
    return OrderBook(
        symbol=symbol,
        exchange=exchange,
        exchange_timestamp=timestamp,
        receive_timestamp=timestamp,
        last_price=(bid + ask) / 2,
        bids=(OrderBookLevel(bid, 1_000),),
        asks=(OrderBookLevel(ask, 1_000),),
        source="INTRANET_REDIS",
    )


def test_cross_border_paper_pcf_excludes_virtual_subscription_cash() -> None:
    proxy = _cross_border_proxy_pcf(_sample_pcf())

    assert [component.stock_code for component in proxy.components] == ["00001"]
    assert proxy.total_record_num == 1


def test_redis_contract_uses_exchange_qualified_codes() -> None:
    interface = CrossBorderSnapshotInterface()
    source = PendingCrossBorderMarketDataSource(
        interface, CrossBorderSnapshotSourceMode.REDIS
    )

    assert interface.instrument_id.key == "SZSE:159920"
    assert interface.redis_etf_field_template == "{etf_code}.SZ"
    assert interface.redis_hk_field_template == "{component_code}.HK"
    assert source.health().status == "FIELD_MAP_PENDING"
    assert "REDIS" in source.health().message


def test_directional_iopv_uses_redis_books_and_excludes_virtual_cash() -> None:
    pcf = _sample_pcf()
    timestamp = datetime(2026, 8, 4, 10, 0)
    snapshot = MarketSnapshot(
        snapshot_timestamp=timestamp,
        etf_order_book=_book("159920", "SZSE", 0.99, 1.00),
        component_order_books={"00001": _book("00001", "HKEX", 10.0, 10.1)},
        official_iopv=0.97,
        data_quality_status=DataQualityStatus.GOOD,
        source_mode="INTRANET_REDIS",
        assembly_blockers=("STOCK_CONNECT_ELIGIBILITY_NOT_VALIDATED",),
    )

    metrics = calculate_cross_border_indicative_metrics(
        snapshot, pcf, HKDCNYQuote(bid=0.86, ask=0.87)
    )

    assert virtual_subscription_cash_component(pcf).stock_code == "159900"
    assert [item.stock_code for item in cross_border_hk_components(pcf)] == [
        "00001"
    ]
    assert metrics.creation_reference_iopv_cny == pytest.approx(
        (100 + 100 * 10.1 * 0.87) / 1_000
    )
    assert metrics.redemption_reference_iopv_cny == pytest.approx(
        (100 + 100 * 10.0 * 0.86) / 1_000
    )
    assert metrics.upstream_official_iopv_cny == pytest.approx(0.97)
    assert metrics.status == "INDICATIVE"
    assert "STOCK_CONNECT_ELIGIBILITY_NOT_VALIDATED" in metrics.blockers


def test_cross_border_dashboard_is_redis_first() -> None:
    root = Path("tmp") / "tests" / uuid4().hex
    root.mkdir(parents=True)
    try:
        dashboard = CrossBorderPanelDashboard(
            root,
            default_date=date(2026, 8, 4),
        )
        template = dashboard.template()

        assert template.title == "港股通跨境ETF研究控制台"
        assert dashboard.pcf_controls.etf_code == "159920"
        assert dashboard.pcf_controls.source_mode.value == "OFFICIAL_DOWNLOAD"
        sidebar_objects = dashboard.sidebar_pcf_layout.objects
        assert sidebar_objects.index(
            dashboard.pcf_controls.download_controls
        ) < sidebar_objects.index(dashboard.pcf_controls.date_picker)
        assert dashboard.pcf_controls.status_table not in sidebar_objects
        assert (
            dashboard.snapshot_source_mode.value
            == CrossBorderSnapshotSourceMode.REDIS
        )
        assert "Redis已设为唯一实时主路径" in dashboard.market_status.object
    finally:
        shutil.rmtree(root, ignore_errors=True)
