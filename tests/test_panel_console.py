from datetime import datetime, timedelta
import json
from pathlib import Path
from uuid import uuid4

import pandas as pd
import panel as pn
import pytest

from etf_arbitrage.data import (
    SubstituteFlag,
    validate_executable_pcf,
)
from etf_arbitrage.dashboard_panel import (
    PanelConsoleDashboard,
    PanelExecutableDashboard,
    PanelReplayDashboard,
)
from etf_arbitrage.dashboard_panel.pcf_controls import PanelPCFControls
from etf_arbitrage.dashboard_panel.console import EXECUTABLE_PAGE, MEAN_PAGE
from etf_arbitrage.executable_config import (
    DataSourceMode,
    RedisSnapshotFormat,
)
from etf_arbitrage.market_data import (
    ExecutableRecordingReplayMarketDataSource,
    RedisMarketDataSource,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_console_separates_mean_reversion_and_executable_pages():
    dashboard = PanelConsoleDashboard(PROJECT_ROOT)
    template = dashboard.template()

    assert template.title == "沪深ETF套利研究控制台"
    assert dashboard.page_select.value == MEAN_PAGE
    assert len(dashboard.main.objects) == 1
    assert dashboard.mean_page is not None
    assert dashboard.executable_page is None
    mean_main = dashboard.main.objects[0]

    dashboard.page_select.value = EXECUTABLE_PAGE

    assert dashboard.page_select.value == EXECUTABLE_PAGE
    assert dashboard.main.objects[0] is not mean_main
    assert dashboard.executable_page is not None
    assert isinstance(dashboard.main.objects[0], pn.Column)
    assert dashboard.executable_page.source_mode in dashboard.sidebar.objects[-1]
    assert not hasattr(dashboard.executable_page, "price_seed_mode")

    executable_main = dashboard.main.objects[0]
    dashboard.page_select.value = MEAN_PAGE

    assert dashboard.main.objects[0] is not executable_main
    assert dashboard.page_select.value == MEAN_PAGE


def test_mean_reversion_page_opens_before_first_observation():
    root = Path("tmp") / "tests" / uuid4().hex
    root.mkdir(parents=True)
    dashboard = PanelReplayDashboard(root)

    try:
        assert dashboard.dataset is None
        assert dashboard.frame.empty
        assert "尚未发现真实行情记录" in dashboard.strategy_error
        assert dashboard.quality_table.value.iloc[0]["状态"] == "尚未发现行情记录"
    finally:
        root.rmdir()


def test_mean_reversion_refresh_keeps_selected_day_and_loads_new_file():
    root = Path("tmp") / "tests" / uuid4().hex
    root.mkdir(parents=True)
    dashboard = PanelReplayDashboard(root)
    target_day = pd.Timestamp("2026-07-24").date()
    dashboard.day_select.value = target_day
    output = (
        root
        / "tmp"
        / "observations"
        / "20260724"
        / "159915.jsonl"
    )
    output.parent.mkdir(parents=True)
    rows = [
        {
            "timestamp": "2026-07-24T09:30:00",
            "ETF_code": "159915",
            "etf_price": 3.60,
            "iopv": 3.59,
            "premium": 3.60 / 3.59 - 1,
        },
        {
            "timestamp": "2026-07-24T09:30:03",
            "ETF_code": "159915",
            "etf_price": 3.61,
            "iopv": 3.60,
            "premium": 3.61 / 3.60 - 1,
        },
    ]
    output.write_text(
        "\n".join(pd.Series(row).to_json() for row in rows) + "\n",
        encoding="utf-8",
    )

    try:
        dashboard.refresh_datasets()

        assert dashboard.day_select.value == target_day
        assert dashboard.dataset is not None
        assert len(dashboard.observations) == 2
        assert "已加载 2 条记录" in dashboard.dataset_hint.object
    finally:
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        root.rmdir()


def _selected_pcf(dashboard: PanelExecutableDashboard):
    pcf, report = validate_executable_pcf(
        dashboard.pcf.selected_path,
        dashboard.pcf.etf_code,
        dashboard.pcf.trading_date,
    )
    assert report.valid
    return pcf


def _write_full_depth_recording(
    path: Path,
    pcf,
    *,
    etf_multiplier: float = 1.0,
    ticks: int = 3,
) -> None:
    active = [
        component
        for component in pcf.components
        if component.component_share > 0
        and component.substitute_flag != SubstituteFlag.MANDATORY
    ]
    base_prices = {
        component.stock_code: 10.0 + index / 100.0
        for index, component in enumerate(active)
    }
    physical = sum(
        component.component_share * base_prices[component.stock_code]
        for component in active
    )
    mandatory = sum(
        component.creation_cash_substitute
        for component in pcf.components
        if component.substitute_flag == SubstituteFlag.MANDATORY
    )
    iopv = (
        physical + mandatory + pcf.estimate_cash_component
    ) / pcf.creation_redemption_unit
    start = datetime.combine(
        pcf.trading_day,
        datetime.min.time(),
    ).replace(hour=9, minute=30)
    rows = []
    for tick in range(ticks):
        timestamp = start + timedelta(seconds=tick)
        component_books = {}
        for component in active:
            middle = base_prices[component.stock_code] * (1.0 + tick * 0.00001)
            component_books[component.stock_code] = _recorded_book(
                component.stock_code,
                component.exchange or pcf.listing_exchange,
                middle,
                timestamp,
                price_step=0.001,
            )
        etf_middle = iopv * etf_multiplier * (1.0 + tick * 0.00001)
        rows.append(
            {
                "schema_version": 1,
                "fingerprint": "test-{}".format(tick),
                "captured_at": timestamp.isoformat(),
                "redis_key": pcf.trading_day.strftime("%Y%m%d"),
                "snapshot_timestamp": timestamp.isoformat(),
                "source_mode": "REDIS_DATE_HASH",
                "pcf_version": pcf.version,
                "pcf_hash": pcf.file_hash,
                "etf_order_book": _recorded_book(
                    pcf.etf_code,
                    pcf.listing_exchange,
                    etf_middle,
                    timestamp,
                    price_step=max(etf_middle * 0.00005, 0.0001),
                ),
                "component_order_books": component_books,
                "fx_quotes": {},
                "raw_records": {},
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            for row in rows
        )
        + "\n",
        encoding="utf-8",
    )


def _recorded_book(
    symbol: str,
    exchange: str,
    middle: float,
    timestamp: datetime,
    *,
    price_step: float,
) -> dict:
    return {
        "symbol": symbol,
        "exchange": exchange,
        "exchange_timestamp": timestamp.isoformat(),
        "receive_timestamp": timestamp.isoformat(),
        "last_price": middle,
        "status": "NORMAL",
        "bids": [
            [middle - price_step * level, 100_000_000.0]
            for level in range(1, 6)
        ],
        "asks": [
            [middle + price_step * level, 100_000_000.0]
            for level in range(1, 6)
        ],
        "source": "test_full_depth_recording",
    }


def test_executable_page_replays_collected_full_depth_snapshot():
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)
    pcf = _selected_pcf(dashboard)
    recording = Path("tmp") / "tests" / "{}.jsonl".format(uuid4().hex)
    _write_full_depth_recording(recording, pcf)
    dashboard.recording_path.value = str(recording)

    try:
        dashboard._step(None)

        assert isinstance(
            dashboard.source,
            ExecutableRecordingReplayMarketDataSource,
        )
        assert len(dashboard.history["snapshots"]) == 1
        assert len(dashboard.snapshot.etf_order_book.bids) == 5
        assert len(dashboard.snapshot.etf_order_book.asks) == 5
        assert (
            len(dashboard.snapshot.component_order_books)
            == len(
                [
                    item
                    for item in pcf.components
                    if item.component_share > 0
                    and item.substitute_flag != SubstituteFlag.MANDATORY
                ]
            )
        )
        assert "原样回放" in dashboard.data_source_message.object
        assert "模拟盘口" not in dashboard.data_source_message.object
    finally:
        if dashboard.source is not None:
            dashboard.source.disconnect()
        recording.unlink(missing_ok=True)


def test_executable_overview_uses_valid_scale_and_solid_purple_iopv():
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)
    price_fields = [
        getattr(getattr(renderer, "glyph", None), "y", None)
        for renderer in dashboard.price_figure.renderers
    ]
    iopv_renderer = next(
        renderer
        for renderer in dashboard.price_figure.renderers
        if getattr(getattr(renderer, "glyph", None), "y", None)
        == "internal_iopv"
    )

    assert dashboard.price_figure.height == 380
    assert dashboard.price_figure.y_range.only_visible
    assert dashboard.price_figure.y_range.range_padding == pytest.approx(0.12)
    assert "lower_bound" not in price_fields
    assert "upper_bound" not in price_fields
    assert iopv_renderer.glyph.line_color == "#7A5C9E"
    assert iopv_renderer.glyph.line_dash == []
    assert iopv_renderer.glyph.line_width == pytest.approx(2.6)
    assert "空白表示深度不足" in dashboard.edge_figure.title.text


def test_executable_overview_hides_unpriced_depth_shortage_values():
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)
    pcf = _selected_pcf(dashboard)
    recording = Path("tmp") / "tests" / "{}.jsonl".format(uuid4().hex)
    _write_full_depth_recording(recording, pcf, ticks=1)
    row = json.loads(recording.read_text(encoding="utf-8"))
    first_symbol = next(iter(row["component_order_books"]))
    shallow = row["component_order_books"][first_symbol]
    shallow["bids"] = [[price, 1.0] for price, _ in shallow["bids"]]
    shallow["asks"] = [[price, 1.0] for price, _ in shallow["asks"]]
    recording.write_text(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    dashboard.recording_path.value = str(recording)

    try:
        dashboard._step(None)

        evaluation = dashboard.result.decision_evaluation
        assert not evaluation.creation.pricing_complete
        assert not evaluation.redemption.pricing_complete
        assert pd.isna(dashboard.market_source.data["lower_bound"][-1])
        assert pd.isna(dashboard.market_source.data["upper_bound"][-1])
        assert pd.isna(dashboard.market_source.data["creation_bps"][-1])
        assert pd.isna(dashboard.market_source.data["redemption_bps"][-1])
        assert dashboard.secondary_metrics.object.count(
            "不可计算（深度不足）"
        ) == 2
        assert "3,000,000" not in dashboard.secondary_metrics.object
    finally:
        if dashboard.source is not None:
            dashboard.source.disconnect()
        recording.unlink(missing_ok=True)


def test_executable_page_shows_adaptive_component_execution_plan():
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)
    pcf = _selected_pcf(dashboard)
    recording = Path("tmp") / "tests" / "{}.jsonl".format(uuid4().hex)
    _write_full_depth_recording(recording, pcf, ticks=1)
    row = json.loads(recording.read_text(encoding="utf-8"))
    symbol = next(
        component.stock_code
        for component in pcf.components
        if component.component_share > 0
        and component.substitute_flag == SubstituteFlag.ALLOWED
    )
    shallow = row["component_order_books"][symbol]
    shallow["bids"] = [[price, 1.0] for price, _ in shallow["bids"]]
    shallow["asks"] = [[price, 1.0] for price, _ in shallow["asks"]]
    recording.write_text(
        json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    dashboard.recording_path.value = str(recording)
    dashboard.optional_cash.value = True

    try:
        dashboard._step(None)

        plan = dashboard.component_plan_table.value
        adaptive = plan[
            (plan["方向"] == "申购")
            & (plan["证券代码"] == symbol)
        ].iloc[0]
        assert adaptive["执行动作"] == "异常成分自适应现金替代"
        assert adaptive["未成交量"] > 0
        assert adaptive["现金替代额"] > 0
        assert dashboard.result.decision_evaluation.creation.basket.fully_filled
        assert "受PCF上限约束" in dashboard.optional_cash.name
        opportunity = dashboard.history["opportunities"][0]
        assert symbol in opportunity["adaptive_cash_symbols"]
        assert "CASH_ADAPTIVE" in opportunity["component_plan_json"]
    finally:
        if dashboard.source is not None:
            dashboard.source.disconnect()
        recording.unlink(missing_ok=True)


def test_executable_page_live_redis_uses_raw_five_levels_without_duplicate_recording(
    monkeypatch,
):
    monkeypatch.setenv("SZ_REDIS_HOST", "127.0.0.1")
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)
    dashboard.source_mode.value = DataSourceMode.REDIS
    dashboard.redis_enabled.value = True

    config = dashboard._build_config()

    assert config.redis.snapshot_format == RedisSnapshotFormat.DATE_HASH
    assert config.redis.number_of_book_levels == 5
    assert config.redis.poll_interval_ms == 3_000
    assert config.redis.trade_date_key == dashboard.pcf.trading_date.strftime(
        "%Y%m%d"
    )
    assert config.redis.recording_path == ""


def test_executable_page_separates_collection_and_simulation_pcf():
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)

    assert dashboard.multi_etf_operations is not None
    assert dashboard.multi_etf_operations.pcf is dashboard.collection_pcf
    assert dashboard.collection_pcf is not dashboard.pcf
    assert dashboard.multi_etf_operations.monitor_etfs.value == ["159915"]
    assert "510300" in dashboard.multi_etf_operations.monitor_etfs.options
    assert "159920" not in dashboard.multi_etf_operations.monitor_etfs.options

    tabs = dashboard.view().select(pn.Tabs)[0]
    assert "数据采集" in tabs._names
    assert "套利模拟配置" in tabs._names
    assert "数据与配置" not in tabs._names


def test_cross_border_page_does_not_expose_mainland_collector():
    dashboard = PanelExecutableDashboard(
        PROJECT_ROOT,
        default_etf="159920",
        cross_border=True,
    )

    assert dashboard.multi_etf_operations is None
    assert "跨境功能暂停" in dashboard.view().objects[0].object


def test_executable_page_checks_and_replays_selected_recording():
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)
    pcf = _selected_pcf(dashboard)
    recording = Path("tmp") / "tests" / "{}.jsonl".format(uuid4().hex)
    _write_full_depth_recording(recording, pcf, ticks=2)
    dashboard.recording_path.value = str(recording)

    try:
        dashboard._inspect_recording(None)
        assert dashboard.file_summary["ETF"] == pcf.etf_code
        assert dashboard.file_summary["ETF买档"] == 5
        assert dashboard.file_summary["ETF卖档"] == 5
        assert "检查通过" in dashboard.file_message.object

        dashboard._ensure_runtime(force=True)
        first = dashboard._advance_once()
        second = dashboard._advance_once()

        assert first.decision_evaluation.timestamp < second.decision_evaluation.timestamp
        assert dashboard.progress.value == 100
        assert dashboard.opening_reference_price is not None
        assert "原样回放" in dashboard.runtime_status.object
    finally:
        if dashboard.source is not None:
            dashboard.source.disconnect()
        recording.unlink(missing_ok=True)


def test_executable_page_can_drive_full_paper_trade_chain_from_recording():
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)
    pcf = _selected_pcf(dashboard)
    recording = Path("tmp") / "tests" / "{}.jsonl".format(uuid4().hex)
    _write_full_depth_recording(
        recording,
        pcf,
        etf_multiplier=1.05,
        ticks=6,
    )
    dashboard.recording_path.value = str(recording)
    dashboard.minimum_amount.value = 0.0
    dashboard.minimum_bps.value = 0.0
    dashboard.safety_bps.value = 0.0
    dashboard.secondary_bps.value = 0.0
    dashboard.depth_haircut.value = 1.0
    dashboard.auto_paper.value = True
    dashboard.record_only.value = False

    try:
        dashboard._ensure_runtime(force=True)
        for _ in range(4):
            dashboard._advance_once()

        assert dashboard.history["orders"]
        assert dashboard.history["fills"]
        assert dashboard.history["primary_market_requests"]
        assert dashboard.history["trades"]
        assert dashboard.history["pnl"]
    finally:
        if dashboard.source is not None:
            dashboard.source.disconnect()
        recording.unlink(missing_ok=True)


def test_executable_data_source_controls_only_offer_full_depth_sources():
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)
    dashboard._configuration_view()

    assert dashboard.source_mode.value == DataSourceMode.EXECUTABLE_REPLAY
    assert dashboard.replay_controls.visible
    assert not dashboard.redis_controls.visible
    assert set(dashboard.source_mode.options.values()) == {
        DataSourceMode.EXECUTABLE_REPLAY,
        DataSourceMode.REDIS,
    }
    assert not hasattr(dashboard, "scenario")
    assert not hasattr(dashboard, "price_seed_mode")
    assert not hasattr(dashboard, "market_upload")

    dashboard.source_mode.value = DataSourceMode.REDIS
    assert not dashboard.replay_controls.visible
    assert dashboard.redis_controls.visible


def test_executable_default_recording_path_matches_collector_output():
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)

    config = dashboard._build_config()

    exported = config.to_dict()
    assert "simulation" not in exported
    assert "file_replay" not in exported
    assert exported["recording_replay"]["path"] == config.file_replay.path
    assert config.file_replay.path.endswith(
        "tmp\\executable_recordings\\{}\\{}.jsonl".format(
            dashboard.pcf.trading_date.strftime("%Y%m%d"),
            dashboard.pcf.etf_code,
        )
    )


def test_executable_parameter_sections_are_visible_collapsible_cards():
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)
    configuration = dashboard._configuration_view()

    cards = configuration.select(pn.Card)

    assert [card.title for card in cards] == [
        "完整五档采集文件回放",
        "Redis实时五档",
        "执行与交易摩擦",
        "账户与一级市场情景",
        "数据质量门禁",
    ]
    assert all(card.collapsible for card in cards)
    assert all(not card.collapsed for card in cards)
    assert not configuration.select(pn.Accordion)


def test_pcf_source_controls_switch_without_mixing_modes():
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)

    assert dashboard.pcf.local_controls.visible
    assert not dashboard.pcf.download_controls.visible
    assert not dashboard.pcf.upload_controls.visible

    dashboard.pcf.source_mode.value = "OFFICIAL_DOWNLOAD"
    assert not dashboard.pcf.local_controls.visible
    assert dashboard.pcf.download_controls.visible
    assert not dashboard.pcf.upload_controls.visible

    dashboard.pcf.source_mode.value = "UPLOAD"
    assert not dashboard.pcf.local_controls.visible
    assert not dashboard.pcf.download_controls.visible
    assert dashboard.pcf.upload_controls.visible


def test_dynamic_szse_etf_gets_automatic_official_pcf_url():
    controls = PanelPCFControls(PROJECT_ROOT, default_etf="159999")

    assert controls.url_input.value == (
        "https://reportdocs.static.szse.cn/files/text/ETFDown/"
        "pcf_{etf_code}_{trade_date}.xml"
    )
