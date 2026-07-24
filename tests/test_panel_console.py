from pathlib import Path
from uuid import uuid4

import pandas as pd
import panel as pn

from etf_arbitrage.dashboard_panel import (
    PanelConsoleDashboard,
    PanelExecutableDashboard,
    PanelReplayDashboard,
)
from etf_arbitrage.dashboard_panel.console import EXECUTABLE_PAGE, MEAN_PAGE
from etf_arbitrage.executable_config import DataSourceMode
from etf_arbitrage.executable_config import SimulationScenario
from etf_arbitrage.market_data import (
    FileReplayMarketDataSource,
    RedisMarketDataSource,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_console_separates_mean_reversion_and_executable_pages():
    dashboard = PanelConsoleDashboard(PROJECT_ROOT)

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

    dashboard._step(None)

    assert len(dashboard.history["snapshots"]) == 1
    assert len(dashboard.history["opportunities"]) == 2
    assert len(dashboard.etf_book_table.value) == int(dashboard.book_levels.value)
    assert not dashboard.component_books_table.value.empty
    assert not dashboard.pcf_components_table.value.empty


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
    assert not dashboard.file_controls.visible
    assert not dashboard.redis_controls.visible

    dashboard.source_mode.value = DataSourceMode.FILE_REPLAY
    assert not dashboard.simulation_controls.visible
    assert dashboard.file_controls.visible
    assert not dashboard.redis_controls.visible

    dashboard.source_mode.value = DataSourceMode.REDIS
    assert not dashboard.simulation_controls.visible
    assert not dashboard.file_controls.visible
    assert dashboard.redis_controls.visible

    dashboard._ensure_runtime(force=True)
    assert isinstance(dashboard.source, RedisMarketDataSource)
    dashboard.source.connect()
    assert dashboard.source.health().status == "DISABLED"


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
