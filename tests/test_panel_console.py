from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd
import panel as pn
import pytest

import etf_arbitrage.dashboard_panel.executable as executable_module
from etf_arbitrage.data import (
    ETFQuote,
    JsonlSnapshotStore,
    MarketSnapshot as RecordedMarketSnapshot,
    SZRedisPriceSeed,
    SZSEPCFParser,
    StockQuote,
    SubstituteFlag,
)
from etf_arbitrage.dashboard_panel import (
    PanelConsoleDashboard,
    PanelExecutableDashboard,
    PanelReplayDashboard,
)
from etf_arbitrage.dashboard_panel.console import EXECUTABLE_PAGE, MEAN_PAGE
from etf_arbitrage.executable_config import (
    DataSourceMode,
    RedisSnapshotFormat,
    SimulationPriceSeedMode,
    SimulationScenario,
)
from etf_arbitrage.market_data import (
    FileReplayMarketDataSource,
    RecordedHistoryMarketDataSource,
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
    assert (
        dashboard.executable_page.price_seed_mode
        in dashboard.sidebar.objects[-1]
    )

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


def test_executable_page_generates_full_depth_simulated_snapshot():
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)
    price_legend_labels = {
        item.label.value for item in dashboard.price_figure.legend[0].items
    }

    dashboard._step(None)

    assert "官方IOPV" not in price_legend_labels
    assert "内部IOPV" in price_legend_labels
    assert len(dashboard.history["snapshots"]) == 1
    assert len(dashboard.history["opportunities"]) == 2
    assert len(dashboard.etf_book_table.value) == int(dashboard.book_levels.value)
    assert not dashboard.component_books_table.value.empty
    assert not dashboard.pcf_components_table.value.empty


def test_executable_page_defaults_live_redis_to_raw_five_level_recording():
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
    assert config.redis.recording_path.endswith(
        "tmp\\executable_recordings\\{}\\{}.jsonl".format(
            config.redis.trade_date_key,
            dashboard.pcf.etf_code,
        )
    )


def test_executable_page_exposes_mainland_multi_etf_collector_only():
    mainland = PanelExecutableDashboard(PROJECT_ROOT)
    cross_border = PanelExecutableDashboard(
        PROJECT_ROOT,
        default_etf="159920",
        cross_border=True,
    )

    assert mainland.multi_etf_operations is not None
    assert mainland.multi_etf_operations.monitor_etfs.value == ["159915"]
    assert "510300" in mainland.multi_etf_operations.monitor_etfs.options
    assert "159920" not in mainland.multi_etf_operations.monitor_etfs.options
    assert "513660" not in mainland.multi_etf_operations.monitor_etfs.options
    assert cross_border.multi_etf_operations is None


def test_executable_page_can_seed_simulation_from_sz_redis_latest_prices(
    monkeypatch,
):
    class FakeSettings:
        @classmethod
        def from_env(cls):
            return object()

    class FakeClient:
        def __init__(self, _settings):
            pass

        def ping(self):
            return True

    def fake_price_seed(_client, pcf, **_kwargs):
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
        iopv = (
            physical + mandatory + pcf.estimate_cash_component
        ) / pcf.creation_redemption_unit
        return SZRedisPriceSeed(
            trade_date=pcf.trading_day.strftime("%Y%m%d"),
            etf_code=pcf.etf_code,
            etf_price=iopv * 1.001,
            component_prices=prices,
        )

    monkeypatch.setattr(executable_module, "SZRedisSettings", FakeSettings)
    monkeypatch.setattr(executable_module, "SZRedisQuotationClient", FakeClient)
    monkeypatch.setattr(
        executable_module,
        "load_pcf_price_seed",
        fake_price_seed,
    )
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)
    dashboard.price_seed_mode.value = SimulationPriceSeedMode.REDIS_LATEST
    dashboard.base_volatility.value = 0.0

    dashboard._ensure_runtime(force=True)
    snapshot = dashboard.source.step()

    assert isinstance(
        dashboard.source,
        executable_module.SimulatedMarketDataSource,
    )
    assert dashboard.source.price_seed_source == "sz_redis_latest_trade"
    assert snapshot.etf_order_book.last_price == pytest.approx(
        dashboard.redis_price_seed.etf_price
    )
    assert "Redis最新成交价" in dashboard.runtime_status.object
    assert "买卖盘深度与后续路径为模拟值" in dashboard.runtime_status.object


def test_executable_page_replays_local_history_with_synthetic_books_without_redis(
    monkeypatch,
):
    recording = Path("tmp") / "tests" / "{}.jsonl".format(uuid4().hex)
    recording.parent.mkdir(parents=True, exist_ok=True)
    pcf = SZSEPCFParser().parse(
        PROJECT_ROOT / "data/pcf/20260722/pcf_159915_20260722.xml"
    )
    active = [
        item
        for item in pcf.components
        if item.component_share > 0
        and item.substitute_flag != SubstituteFlag.MANDATORY
    ]
    store = JsonlSnapshotStore(recording)
    for tick, etf_price in enumerate((3.600, 3.610)):
        timestamp = datetime(2026, 7, 22, 9, 30, 3 * tick)
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
                stock_quotes={
                    component.stock_code: StockQuote(
                        timestamp=timestamp,
                        stock_code=component.stock_code,
                        last_price=10.0 + index / 100.0 + tick / 1000.0,
                        previous_close=9.9 + index / 100.0,
                        volume=1_000,
                    )
                    for index, component in enumerate(active)
                },
            )
        )

    try:
        monkeypatch.setattr(
            executable_module,
            "_load_redis_price_seed",
            lambda *_args, **_kwargs: pytest.fail("Redis must not be accessed"),
        )
        dashboard = PanelExecutableDashboard(PROJECT_ROOT)
        dashboard.pcf.date_picker.value = datetime(2026, 7, 22).date()
        dashboard.price_seed_mode.value = SimulationPriceSeedMode.LOCAL_RECORDING
        dashboard.local_recording_path.value = str(recording)

        dashboard._ensure_runtime(force=True)
        first = dashboard._advance_once()
        second = dashboard._advance_once()

        assert isinstance(dashboard.source, RecordedHistoryMarketDataSource)
        assert dashboard.redis_price_seed is None
        assert dashboard.history["snapshots"][0]["etf_last"] == pytest.approx(3.600)
        assert dashboard.history["snapshots"][1]["etf_last"] == pytest.approx(3.610)
        assert first.decision_evaluation.timestamp < second.decision_evaluation.timestamp
        assert len(dashboard.snapshot.etf_order_book.bids) == int(
            dashboard.book_levels.value
        )
        assert dashboard.opening_reference_price == pytest.approx(3.600)
        assert dashboard.market_source.data["etf_last_rel_bps"] == pytest.approx(
            [0.0, (3.610 / 3.600 - 1.0) * 10_000.0]
        )
        assert dashboard.market_source.data["etf_bid_rel_bps"][0] < 0
        assert dashboard.market_source.data["etf_ask_rel_bps"][0] > 0
        assert "3.6000=0 bp" in dashboard.price_figure.title.text
        assert dashboard.opening_source.data["price"] == pytest.approx([3.600])
        assert dashboard.opening_span.location == pytest.approx(3.600)
        assert dashboard.opening_span.visible is True
        assert all(
            value is None or value > 0
            for value in dashboard.market_source.data["official_iopv"]
        )
        assert dashboard.progress.name == "回放进度 2/2"
        assert dashboard.progress.value == 100
        assert "逐条回放本地历史数据" in dashboard.runtime_status.object
        assert "Bid/Ask与多档深度为模拟值" in dashboard.runtime_status.object

        dashboard._clear_sources()
        assert dashboard.opening_reference_price is None
        assert dashboard.opening_source.data["price"] == [None]
        assert dashboard.opening_span.visible is False
        assert dashboard.market_source.data["etf_last_rel_bps"] == []
        assert dashboard.price_figure.title.text == (
            "ETF价格、盘口与IOPV（实际价格窄幅缩放）"
        )
        dashboard.source.disconnect()
    finally:
        recording.unlink(missing_ok=True)


def test_executable_page_uploads_and_replays_market_file():
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)
    frame = pd.DataFrame(
        [
            {
                "timestamp": "2026-07-23T01:30:00Z",
                "symbol": "159915",
                "is_etf": True,
                "last_price": 1.000,
                "bid1_price": 0.999,
                "bid1_quantity": 100_000,
                "ask1_price": 1.001,
                "ask1_quantity": 100_000,
                "official_iopv": 1.000,
                "internal_iopv": 1.000,
            },
            {
                "timestamp": "2026-07-23T01:30:01Z",
                "symbol": "159915",
                "is_etf": True,
                "last_price": 1.001,
                "bid1_price": 1.000,
                "bid1_quantity": 100_000,
                "ask1_price": 1.002,
                "ask1_quantity": 100_000,
                "official_iopv": 1.000,
                "internal_iopv": 1.000,
            },
        ]
    )
    dashboard.source_mode.value = DataSourceMode.FILE_REPLAY
    dashboard.market_upload.param.update(
        value=frame.to_csv(index=False).encode("utf-8"),
        filename="uploaded.csv",
    )

    dashboard._load_market_data(None)
    assert "历史行情回放" in dashboard.runtime_status.object
    dashboard._step(None)

    assert isinstance(dashboard.source, FileReplayMarketDataSource)
    assert dashboard.file_summary.snapshot_count == 2
    assert len(dashboard.history["snapshots"]) == 1
    assert "已推进一个快照" in dashboard.runtime_status.object


def test_executable_page_can_drive_full_paper_trade_chain():
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)
    dashboard.scenario.value = SimulationScenario.PREMIUM_SHOCK
    dashboard.premium_shock.value = 100.0
    dashboard.minimum_amount.value = 0.0
    dashboard.minimum_bps.value = 0.0
    dashboard.safety_bps.value = 0.0
    dashboard.secondary_bps.value = 0.0
    dashboard.depth_per_level.value = 10.0
    dashboard.auto_paper.value = True
    dashboard.record_only.value = False
    dashboard._ensure_runtime(force=True)

    for _ in range(5):
        dashboard._advance_once()

    assert dashboard.history["orders"]
    assert dashboard.history["fills"]
    assert dashboard.history["primary_market_requests"]
    assert dashboard.history["trades"]
    assert dashboard.history["pnl"]


def test_executable_data_source_controls_are_independent():
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)
    dashboard._configuration_view()

    assert dashboard.simulation_controls.visible
    assert dashboard.price_seed_mode.visible
    assert not dashboard.file_controls.visible
    assert not dashboard.redis_controls.visible

    dashboard.source_mode.value = DataSourceMode.FILE_REPLAY
    assert not dashboard.simulation_controls.visible
    assert not dashboard.price_seed_mode.visible
    assert dashboard.file_controls.visible
    assert not dashboard.redis_controls.visible

    dashboard.source_mode.value = DataSourceMode.REDIS
    assert not dashboard.simulation_controls.visible
    assert not dashboard.price_seed_mode.visible
    assert not dashboard.file_controls.visible
    assert dashboard.redis_controls.visible

    dashboard._ensure_runtime(force=True)
    assert isinstance(dashboard.source, RedisMarketDataSource)
    dashboard.source.connect()
    assert dashboard.source.health().status == "DISABLED"


def test_executable_sidebar_names_local_recording_without_implying_live_redis():
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)

    assert dashboard.source_mode.options["模拟行情（本地/合成）"] == (
        DataSourceMode.SIMULATED
    )
    assert dashboard.price_seed_mode.options[
        "本地历史数据（逐条回放）"
    ] == SimulationPriceSeedMode.AUTO_LOCAL
    assert dashboard.price_seed_mode.value == SimulationPriceSeedMode.AUTO_LOCAL

    dashboard.runtime_status.object = (
        '<div class="exec-status">旧的Redis连接错误</div>'
    )
    dashboard.price_seed_mode.value = SimulationPriceSeedMode.SYNTHETIC
    dashboard.price_seed_mode.value = SimulationPriceSeedMode.AUTO_LOCAL

    assert "不连接内网Redis" in dashboard.runtime_status.object
    assert "逐条保留ETF与成分股Last历史轨迹" in dashboard.runtime_status.object
    assert "旧的Redis连接错误" not in dashboard.runtime_status.object


def test_executable_parameter_sections_are_visible_collapsible_cards():
    dashboard = PanelExecutableDashboard(PROJECT_ROOT)
    configuration = dashboard._configuration_view()

    cards = configuration.select(pn.Card)

    assert [card.title for card in cards] == [
        "境内多ETF日内五档采集",
        "模拟行情高级参数",
        "执行参数",
        "账户与一级市场情景",
        "数据质量阈值",
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
