"""Panel control surface for the paper-only executable arbitrage simulator."""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import timedelta
from io import BytesIO
import json
from math import isfinite
import os
from pathlib import Path
from typing import Any, Dict, Optional, Union

import pandas as pd
import panel as pn
from bokeh.models import (
    ColumnDataSource,
    CrosshairTool,
    CustomJSTickFormatter,
    DataRange1d,
    HoverTool,
    LinearAxis,
    Span,
)
from bokeh.plotting import figure

from etf_arbitrage.data import (
    SZRedisSettings,
    validate_executable_pcf,
)
from etf_arbitrage.engine import PaperArbitrageEngine, PaperEngineResult
from etf_arbitrage.executable_config import (
    AccountConfig,
    CostConfig,
    DataQualityConfig,
    DataSourceMode,
    DirectionSelection,
    ExecutionConfig,
    ExecutionMode,
    ExecutionScenario,
    FileReplayConfig,
    PaperArbitrageConfig,
    PrimaryMarketConfig,
    RedisConfig,
    RedisSnapshotFormat,
)
from etf_arbitrage.market_data import (
    ExecutableRecordingReplayMarketDataSource,
    MarketDataSource,
    NoNewSnapshotError,
    RedisMarketDataSource,
    cross_border_hk_components,
)
from etf_arbitrage.reporting import RunRecorder

from .executable_operations import ExecutableMultiETFOperations
from .pcf_controls import PanelPCFControls


ACCENT = "#15616D"
POSITIVE = "#C44536"
NEGATIVE = "#3A6EA5"
ETF_COLOR = "#202A2E"
IOPV_COLOR = "#7A5C9E"
BID_COLOR = "#2F6B4F"
ASK_COLOR = "#C44536"

STATUS_LABELS = {
    "RUNNING": "运行中",
    "READY": "就绪",
    "COMPLETED": "已完成",
    "DISCONNECTED": "未连接",
    "DISABLED": "已禁用",
    "ERROR": "异常",
}

HISTORY_TABLES = (
    "snapshots",
    "opportunities",
    "rejected_opportunities",
    "orders",
    "fills",
    "primary_market_requests",
    "trades",
    "pnl",
    "data_quality_events",
)

EXECUTABLE_CSS = """
.exec-metrics {
  display: grid;
  grid-template-columns: repeat(6, minmax(120px, 1fr));
  gap: 10px;
}
.exec-metric {
  min-height: 78px;
  padding: 11px 13px;
  border: 1px solid #d8dee1;
  border-top: 3px solid var(--metric-accent, #15616d);
  border-radius: 4px;
  background: #ffffff;
}
.exec-label {
  color: #657074;
  font-size: 12px;
}
.exec-value {
  margin-top: 7px;
  color: #202a2e;
  font-size: 20px;
  font-weight: 650;
}
.exec-status {
  padding: 8px 10px;
  border-left: 3px solid #15616d;
  background: #f4f7f7;
  color: #374147;
}
@media (max-width: 1100px) {
  .exec-metrics { grid-template-columns: repeat(3, minmax(120px, 1fr)); }
}
"""


class PanelExecutableDashboard:
    """Session-local executable-arbitrage simulator and replay controller."""

    def __init__(
        self,
        project_root: Union[Path, str],
        default_etf: str = "159915",
        cross_border: bool = False,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.run_root = self.project_root / "data" / "runs"
        self.default_etf = str(default_etf).zfill(6)
        self.cross_border = bool(cross_border)
        self.pcf = PanelPCFControls(
            self.project_root,
            default_etf=self.default_etf,
        )
        self.collection_pcf = PanelPCFControls(
            self.project_root,
            default_etf=self.default_etf,
        )
        self.multi_etf_operations = (
            None
            if self.cross_border
            else ExecutableMultiETFOperations(
                self.project_root,
                self.collection_pcf,
                default_etfs=(self.default_etf,),
            )
        )
        self.source: Optional[MarketDataSource] = None
        self.file_summary = None
        self.engine: Optional[PaperArbitrageEngine] = None
        self.snapshot = None
        self.result: Optional[PaperEngineResult] = None
        self.config: Optional[PaperArbitrageConfig] = None
        self.pcf_document = None
        self.pcf_report = None
        self.opening_reference_price: Optional[float] = None
        self.callback = None
        self.history = _new_history()

        self._build_data_widgets()
        self._build_execution_widgets()
        self._build_runtime_widgets()
        self._build_output_models()
        self._wire_events()
        self._source_mode_changed(None)
        if self.cross_border:
            self.optional_cash.value = False
            self.optional_cash.disabled = True
            self.optional_cash.name = "跨境套利当前暂停"
        self._refresh_pcf_views()
        self._update_views()

    def _build_data_widgets(self) -> None:
        self.source_mode = pn.widgets.Select(
            label="套利模拟行情来源",
            options={
                "完整五档采集文件回放": DataSourceMode.EXECUTABLE_REPLAY,
                "Redis实时五档（纸面模拟）": DataSourceMode.REDIS,
            },
            value=DataSourceMode.EXECUTABLE_REPLAY,
        )
        self.recording_path = pn.widgets.TextInput(
            label="完整五档采集文件",
            value="",
            placeholder=(
                "留空自动使用 "
                "tmp/executable_recordings/交易日/ETF代码.jsonl"
            ),
        )
        self.file_speed = _float_input(
            "回放速度（倍）", 1.0, 0.1, 100.0, 0.1
        )
        self.file_step_ms = _int_input(
            "采集快照基准间隔（毫秒）", 3000, 10, 60_000, 10
        )
        self.inspect_recording_button = pn.widgets.Button(
            label="检查采集文件",
            icon="file-search",
            color="primary",
        )
        self.file_message = pn.pane.HTML("", sizing_mode="stretch_width")
        self.file_summary_table = pn.widgets.Tabulator(
            pd.DataFrame(),
            show_index=False,
            disabled=True,
            height=140,
            sizing_mode="stretch_width",
        )
        self.data_source_message = pn.pane.HTML(
            '<div class="exec-status">'
            "默认回放数据采集页面保存的原始五档快照；不会补造盘口、"
            "缺失成分或异常行情。"
            "</div>",
            sizing_mode="stretch_width",
            stylesheets=[EXECUTABLE_CSS],
        )

        self.redis_enabled = pn.widgets.Toggle(
            label="启用Redis实时纸面模拟",
            value=True,
        )
        self.redis_poll_interval = _int_input(
            "Redis轮询间隔（毫秒）", 3_000, 250, 60_000, 250
        )
        self.redis_timeout = _float_input(
            "Redis连接超时（秒）", 2.0, 0.1, 60.0, 0.1
        )

    def _build_execution_widgets(self) -> None:
        self.direction = pn.widgets.Select(
            label="套利方向",
            options={
                "双向": DirectionSelection.BOTH,
                "仅申购套利": DirectionSelection.CREATION_ONLY,
                "仅赎回套利": DirectionSelection.REDEMPTION_ONLY,
            },
            value=DirectionSelection.BOTH,
        )
        self.execution_mode = pn.widgets.Select(
            label="执行模式",
            options={
                "库存锁定": ExecutionMode.INVENTORY_LOCKED,
                "顺序执行且不借券": ExecutionMode.SEQUENTIAL_NO_BORROW,
            },
            value=ExecutionMode.INVENTORY_LOCKED,
        )
        self.execution_scenario = pn.widgets.Select(
            label="执行情景",
            options={
                "乐观": ExecutionScenario.OPTIMISTIC,
                "基准": ExecutionScenario.BASE,
                "压力": ExecutionScenario.STRESS,
            },
            value=ExecutionScenario.BASE,
        )
        self.cu_count = _int_input("CU数量", 1, 1, 100, 1)
        self.max_cu = _int_input("单笔最大CU", 1, 1, 100, 1)
        self.max_daily_cu = _int_input("当日最大CU", 10, 1, 10_000, 1)
        self.minimum_amount = _float_input(
            "最低利润金额", 100.0, 0.0, 1_000_000.0, 10.0
        )
        self.minimum_bps = _float_input(
            "最低利润（bp）", 1.0, 0.0, 1000.0, 0.5
        )
        self.safety_bps = _float_input(
            "安全缓冲（bp）", 3.0, 0.0, 1000.0, 0.5
        )
        self.secondary_bps = _float_input(
            "二级市场成本（bp）", 3.0, 0.0, 100.0, 0.5
        )
        self.primary_bps = _float_input(
            "申赎费（bp）", 0.0, 0.0, 100.0, 0.5
        )
        self.decision_latency = _int_input(
            "决策延迟（毫秒）", 50, 0, 60_000, 10
        )
        self.order_latency = _int_input(
            "订单延迟（毫秒）", 50, 0, 60_000, 10
        )
        self.primary_latency = _int_input(
            "申赎确认延迟（毫秒）", 500, 0, 600_000, 100
        )
        self.depth_haircut = _float_input(
            "深度折减", 0.90, 0.01, 1.0, 0.01
        )
        self.optional_cash = pn.widgets.Checkbox(
            label="异常成分自适应现金替代（受PCF上限约束）",
            value=False,
        )
        self.auto_paper = pn.widgets.Toggle(
            label="自动影子交易",
            value=False,
        )
        self.record_only = pn.widgets.Checkbox(
            label="只记录机会",
            value=True,
        )

        self.borrowing_bps = _float_input(
            "借券成本（bp）", 0.0, 0.0, 10_000.0, 0.5
        )
        self.financing_bps = _float_input(
            "资金成本（bp）", 0.0, 0.0, 10_000.0, 0.5
        )
        self.initial_cash = _float_input(
            "初始虚拟现金",
            50_000_000.0,
            0.0,
            10_000_000_000.0,
            100_000.0,
        )
        self.initial_etf_inventory = _int_input(
            "初始ETF库存", 0, 0, 1_000_000_000, 100_000
        )
        self.etf_borrow_limit = _int_input(
            "ETF虚拟券源上限", 10_000_000, 0, 1_000_000_000, 100_000
        )
        self.allow_stock_borrow = pn.widgets.Checkbox(
            label="允许成分股虚拟借券",
            value=True,
        )
        self.primary_reject_probability = _float_input(
            "模拟申赎拒绝概率", 0.0, 0.0, 1.0, 0.01
        )
        self.cash_component_error = _float_input(
            "最终现金差额误差",
            0.0,
            -1_000_000.0,
            1_000_000.0,
            100.0,
        )
        self.final_cash_delay = _int_input(
            "最终退补款延迟（毫秒）", 1000, 0, 86_400_000, 100
        )

        self.max_age = _int_input(
            "单证券最长未更新（毫秒）", 120000, 1, 600_000, 1000
        )
        self.max_source_age = _int_input(
            "行情源水位最大延迟（毫秒）", 15000, 1, 600_000, 1000
        )
        self.max_skew = _int_input(
            "非原子源事件时差（毫秒）", 5000, 1, 600_000, 100
        )
        self.max_missing = _float_input(
            "最大缺失权重", 0.01, 0.0, 1.0, 0.01
        )
        self.max_stale = _float_input(
            "最大陈旧权重", 0.05, 0.0, 1.0, 0.01
        )
        self.max_suspended = _float_input(
            "最大停牌权重", 0.05, 0.0, 1.0, 0.01
        )
        self.max_limit_up = _float_input(
            "最大涨停无卖盘权重", 0.10, 0.0, 1.0, 0.01
        )
        self.max_limit_down = _float_input(
            "最大跌停无买盘权重", 0.10, 0.0, 1.0, 0.01
        )
        self.max_iopv_error = _float_input(
            "最大IOPV误差（bp）", 30.0, 0.0, 10_000.0, 1.0
        )
        self.kill_switch = pn.widgets.Checkbox(
            label="启用风控熔断",
            value=True,
        )

    def _build_runtime_widgets(self) -> None:
        self.apply_button = pn.widgets.Button(
            label="应用配置并重置",
            icon="settings",
            color="primary",
        )
        self.start_button = pn.widgets.Button(
            label="开始",
            icon="player-play",
            color="primary",
        )
        self.pause_button = pn.widgets.Button(
            label="暂停",
            icon="player-pause",
        )
        self.step_button = pn.widgets.Button(
            label="单步",
            icon="player-skip-forward",
        )
        self.reset_button = pn.widgets.Button(
            label="重置",
            icon="refresh",
        )
        self.runtime_status = pn.pane.HTML(
            "",
            sizing_mode="stretch_width",
            stylesheets=[EXECUTABLE_CSS],
        )
        self.runtime_metrics = pn.pane.HTML(
            "",
            sizing_mode="stretch_width",
            stylesheets=[EXECUTABLE_CSS],
        )
        self.secondary_metrics = pn.pane.HTML(
            "",
            sizing_mode="stretch_width",
            stylesheets=[EXECUTABLE_CSS],
        )
        self.progress = pn.indicators.Progress(
            label="模拟/回放进度",
            value=0,
            max=100,
            sizing_mode="stretch_width",
        )

    def _build_output_models(self) -> None:
        self.market_source = ColumnDataSource(
            data={
                "timestamp": [],
                "etf_last": [],
                "etf_bid": [],
                "etf_ask": [],
                "official_iopv": [],
                "internal_iopv": [],
                "lower_bound": [],
                "upper_bound": [],
                "etf_last_rel_bps": [],
                "etf_bid_rel_bps": [],
                "etf_ask_rel_bps": [],
                "official_iopv_rel_bps": [],
                "internal_iopv_rel_bps": [],
                "lower_bound_rel_bps": [],
                "upper_bound_rel_bps": [],
                "creation_bps": [],
                "redemption_bps": [],
            }
        )
        self.opening_source = ColumnDataSource(data={"price": [None]})
        self.pnl_source = ColumnDataSource(
            data={"timestamp": [], "cumulative_pnl": []}
        )
        self.price_figure = self._build_price_figure()
        self.edge_figure = self._build_edge_figure()
        self.edge_figure.x_range = self.price_figure.x_range
        self.pnl_figure = self._build_pnl_figure()

        self.pcf_header_table = _table(height=170)
        self.pcf_components_table = _table(height=430)
        self.etf_book_table = _table(height=250)
        self.component_books_table = _table(height=390)
        self.component_plan_table = _table(height=430)
        self.basket_table = _table(height=190)
        self.opportunities_table = _table(height=350)
        self.capacity_table = _table(height=180)
        self.funnel_table = _table(height=230)
        self.orders_table = _table(height=230)
        self.fills_table = _table(height=230)
        self.primary_table = _table(height=230)
        self.pnl_table = _table(height=300)
        self.quality_table = _table(height=330)
        self.health_table = _table(height=170)
        self.history_table = _table(height=350)
        self.config_pane = pn.pane.JSON(
            {},
            depth=3,
            sizing_mode="stretch_width",
        )
        self.export_select = pn.widgets.Select(
            label="导出数据表",
            options=list(HISTORY_TABLES),
            value="snapshots",
        )
        self.export_csv = pn.widgets.FileDownload(
            label="下载CSV",
            icon="download",
            callback=self._download_csv,
            filename="snapshots.csv",
            color="primary",
        )
        self.export_parquet = pn.widgets.FileDownload(
            label="下载Parquet",
            icon="download",
            callback=self._download_parquet,
            filename="snapshots.parquet",
        )
        self.export_config = pn.widgets.FileDownload(
            label="导出当前配置JSON",
            icon="download",
            callback=self._download_config,
            filename="executable_arbitrage_config.json",
        )
        self.save_run_button = pn.widgets.Button(
            label="保存完整运行数据",
            icon="device-floppy",
            color="primary",
        )
        self.save_message = pn.pane.HTML("", sizing_mode="stretch_width")

    def _wire_events(self) -> None:
        self.source_mode.param.watch(self._source_mode_changed, "value")
        self.inspect_recording_button.on_click(self._inspect_recording)
        self.apply_button.on_click(self._apply_config)
        self.start_button.on_click(self._start)
        self.pause_button.on_click(self._pause)
        self.step_button.on_click(self._step)
        self.reset_button.on_click(self._reset)
        self.export_select.param.watch(self._export_table_changed, "value")
        self.save_run_button.on_click(self._save_run)
        self.pcf.on_change(self._pcf_changed)

    def sidebar_controls(self):
        return pn.Column(
            pn.pane.Markdown("### 实盘套利模拟"),
            self.source_mode,
            self.apply_button,
            pn.Row(self.start_button, self.pause_button),
            pn.Row(self.step_button, self.reset_button),
            self.progress,
            sizing_mode="stretch_width",
        )

    def view(self):
        runtime = pn.Column(
            self.runtime_metrics,
            self.secondary_metrics,
            self.runtime_status,
            pn.pane.Bokeh(self.price_figure, sizing_mode="stretch_width"),
            pn.pane.Bokeh(self.edge_figure, sizing_mode="stretch_width"),
            sizing_mode="stretch_width",
        )
        boundary = []
        if self.cross_border:
            boundary.append(
                pn.pane.HTML(
                    '<div class="exec-status"><b>跨境功能暂停：</b>'
                    "当前只保留境内ETF的完整五档采集、回放与纸面套利模拟。"
                    "</div>",
                    sizing_mode="stretch_width",
                    stylesheets=[EXECUTABLE_CSS],
                )
            )
        return pn.Column(
            *boundary,
            pn.Tabs(
                ("实时总览", runtime),
                ("数据采集", self._data_collection_view()),
                ("套利模拟配置", self._configuration_view()),
                ("PCF", self._pcf_view()),
                ("盘口与篮子", self._book_view()),
                ("套利机会", self._opportunity_view()),
                ("订单与申赎", self._orders_view()),
                ("损益", self._pnl_view()),
                ("数据质量与日志", self._quality_view()),
                ("历史数据与导出", self._history_view()),
                dynamic=True,
                sizing_mode="stretch_width",
            ),
            sizing_mode="stretch_width",
        )

    def stop_runtime(self) -> None:
        if self.callback is not None and self.callback.running:
            self.callback.stop()
        if self.source is not None:
            self.source.stop()
        if self.multi_etf_operations is not None:
            self.multi_etf_operations.stop_runtime()

    def _data_collection_view(self):
        if self.multi_etf_operations is None:
            return pn.Column(
                pn.pane.Markdown("## 数据采集"),
                pn.pane.HTML(
                    '<div class="exec-status">当前仅开放境内ETF五档采集。</div>',
                    stylesheets=[EXECUTABLE_CSS],
                ),
                sizing_mode="stretch_width",
            )
        return pn.Column(
            pn.pane.Markdown("## 数据采集"),
            pn.pane.HTML(
                '<div class="exec-status">'
                "这里仅负责选择PCF、并行采集多只ETF并保存原始五档文件；"
                "不会在后台自动成交或改动套利模拟账户。"
                "</div>",
                stylesheets=[EXECUTABLE_CSS],
                sizing_mode="stretch_width",
            ),
            self.collection_pcf.view(),
            self.multi_etf_operations.view(),
            sizing_mode="stretch_width",
        )

    def _configuration_view(self):
        self.replay_controls = pn.Column(
            self.recording_path,
            pn.Row(self.file_speed, self.file_step_ms),
            self.inspect_recording_button,
            self.file_message,
            self.file_summary_table,
            sizing_mode="stretch_width",
        )
        self.redis_controls = pn.Column(
            self.redis_enabled,
            pn.Row(self.redis_poll_interval, self.redis_timeout),
            pn.pane.Markdown(
                "Redis连接读取当前进程的 SZ_REDIS_* 环境配置。"
                "实时纸面模拟不重复写采集文件；原始文件由“数据采集”页面统一保存。"
            ),
            sizing_mode="stretch_width",
        )
        self._source_mode_changed(None)

        execution_controls = pn.Column(
            pn.Row(self.direction, self.execution_mode, self.execution_scenario),
            pn.Row(self.cu_count, self.max_cu, self.max_daily_cu),
            pn.Row(
                self.minimum_amount,
                self.minimum_bps,
                self.safety_bps,
            ),
            pn.Row(self.secondary_bps, self.primary_bps),
            pn.Row(
                self.decision_latency,
                self.order_latency,
                self.primary_latency,
            ),
            pn.Row(self.depth_haircut, self.optional_cash),
            pn.pane.Markdown(
                "勾选后，仅在某一方向无法按五档完成实物成交、且PCF标记为"
                "“允许替代”时，才对该成分改用现金替代；停板解除或深度恢复后"
                "会自动回到实物交易。超过PCF最大现金替代比例时仍会阻断。"
            ),
            pn.Row(self.auto_paper, self.record_only),
            sizing_mode="stretch_width",
        )
        account_controls = pn.Column(
            pn.Row(self.borrowing_bps, self.financing_bps),
            pn.Row(self.initial_cash, self.initial_etf_inventory),
            pn.Row(self.etf_borrow_limit, self.allow_stock_borrow),
            pn.Row(
                self.primary_reject_probability,
                self.cash_component_error,
                self.final_cash_delay,
            ),
            sizing_mode="stretch_width",
        )
        quality_controls = pn.Column(
            pn.Row(
                self.max_source_age,
                self.max_age,
                self.max_skew,
                self.max_iopv_error,
            ),
            pn.Row(self.max_missing, self.max_stale, self.max_suspended),
            pn.Row(self.max_limit_up, self.max_limit_down, self.kill_switch),
            sizing_mode="stretch_width",
        )
        return pn.Column(
            pn.pane.Markdown("## 套利模拟配置"),
            pn.pane.HTML(
                '<div class="exec-status">'
                "选择一个ETF及同交易日PCF，再使用该ETF的完整五档采集文件回放；"
                "也可切换为Redis实时纸面模拟。所有订单仍为纸面订单。"
                "</div>",
                stylesheets=[EXECUTABLE_CSS],
                sizing_mode="stretch_width",
            ),
            self.source_mode,
            self.data_source_message,
            self.pcf.view(),
            pn.Card(
                self.replay_controls,
                title="完整五档采集文件回放",
                collapsed=False,
                collapsible=True,
                sizing_mode="stretch_width",
            ),
            pn.Card(
                self.redis_controls,
                title="Redis实时五档",
                collapsed=False,
                collapsible=True,
                sizing_mode="stretch_width",
            ),
            pn.Card(
                execution_controls,
                title="执行与交易摩擦",
                collapsed=False,
                collapsible=True,
                sizing_mode="stretch_width",
            ),
            pn.Card(
                account_controls,
                title="账户与一级市场情景",
                collapsed=False,
                collapsible=True,
                sizing_mode="stretch_width",
            ),
            pn.Card(
                quality_controls,
                title="数据质量门禁",
                collapsed=False,
                collapsible=True,
                sizing_mode="stretch_width",
            ),
            sizing_mode="stretch_width",
        )

    def _pcf_view(self):
        return pn.Column(
            pn.pane.Markdown("## PCF申赎清单"),
            self.pcf_header_table,
            pn.pane.Markdown("#### 成分证券与现金替代"),
            self.pcf_components_table,
            sizing_mode="stretch_width",
        )

    def _book_view(self):
        return pn.Column(
            pn.pane.Markdown("## ETF盘口"),
            self.etf_book_table,
            pn.pane.Markdown("## 成分股盘口"),
            self.component_books_table,
            pn.pane.Markdown("## 逐成分执行方案（异常项优先）"),
            self.component_plan_table,
            pn.pane.Markdown("## 申购/赎回篮子"),
            self.basket_table,
            sizing_mode="stretch_width",
        )

    def _opportunity_view(self):
        return pn.Column(
            pn.pane.Markdown("## 套利机会记录"),
            self.opportunities_table,
            pn.pane.Markdown("## 容量"),
            self.capacity_table,
            pn.pane.Markdown("## 机会漏斗"),
            self.funnel_table,
            sizing_mode="stretch_width",
        )

    def _orders_view(self):
        return pn.Column(
            pn.pane.Markdown("## 模拟订单"),
            self.orders_table,
            pn.pane.Markdown("## 模拟成交"),
            self.fills_table,
            pn.pane.Markdown("## 一级市场申赎"),
            self.primary_table,
            sizing_mode="stretch_width",
        )

    def _pnl_view(self):
        return pn.Column(
            pn.pane.Markdown("## 模拟损益"),
            pn.pane.Bokeh(self.pnl_figure, sizing_mode="stretch_width"),
            self.pnl_table,
            sizing_mode="stretch_width",
        )

    def _quality_view(self):
        return pn.Column(
            pn.pane.Markdown("## 数据质量事件"),
            self.quality_table,
            pn.pane.Markdown("## 数据源健康状态"),
            self.health_table,
            sizing_mode="stretch_width",
        )

    def _history_view(self):
        return pn.Column(
            pn.pane.Markdown("## 历史数据与导出"),
            self.export_select,
            self.history_table,
            pn.Row(self.export_csv, self.export_parquet, self.export_config),
            self.config_pane,
            self.save_run_button,
            self.save_message,
            sizing_mode="stretch_width",
        )

    def _build_config(self) -> PaperArbitrageConfig:
        try:
            redis_settings = SZRedisSettings.from_env()
        except ValueError as exc:
            if self.source_mode.value == DataSourceMode.REDIS:
                raise ValueError(
                    "Redis实时纸面模拟需要先设置SZ_REDIS_HOST"
                ) from exc
            redis_settings = SZRedisSettings(host="localhost")
        recording_path = _resolve_executable_recording_path(
            self.project_root,
            self.pcf.trading_date,
            self.pcf.etf_code,
            self.recording_path.value.strip(),
        )
        return PaperArbitrageConfig(
            etf_code=self.pcf.etf_code,
            data_source=self.source_mode.value,
            redis=RedisConfig(
                enabled=(
                    self.source_mode.value == DataSourceMode.REDIS
                    and bool(self.redis_enabled.value)
                ),
                host=redis_settings.host,
                port=redis_settings.port,
                db=redis_settings.db,
                password=redis_settings.password,
                snapshot_format=RedisSnapshotFormat.DATE_HASH,
                trade_date_key=self.pcf.trading_date.strftime("%Y%m%d"),
                number_of_book_levels=5,
                poll_interval_ms=int(self.redis_poll_interval.value),
                recording_path="",
                socket_timeout_seconds=float(self.redis_timeout.value),
            ),
            file_replay=FileReplayConfig(
                path=str(recording_path),
                fixed_step_ms=int(self.file_step_ms.value),
                playback_speed=float(self.file_speed.value),
            ),
            costs=CostConfig(
                secondary_market_bps=float(self.secondary_bps.value),
                creation_fee_bps=float(self.primary_bps.value),
                redemption_fee_bps=float(self.primary_bps.value),
                borrowing_bps=float(self.borrowing_bps.value),
                financing_bps=float(self.financing_bps.value),
            ),
            execution=ExecutionConfig(
                direction=self.direction.value,
                mode=self.execution_mode.value,
                scenario=self.execution_scenario.value,
                cu_count=int(self.cu_count.value),
                maximum_cu_per_trade=int(self.max_cu.value),
                maximum_daily_cu=int(self.max_daily_cu.value),
                decision_latency_ms=int(self.decision_latency.value),
                order_latency_ms=int(self.order_latency.value),
                primary_market_latency_ms=int(self.primary_latency.value),
                depth_haircut=float(self.depth_haircut.value),
                safety_buffer_bps=float(self.safety_bps.value),
                minimum_profit_bps=float(self.minimum_bps.value),
                minimum_profit_amount=float(self.minimum_amount.value),
                optional_cash_substitution=bool(self.optional_cash.value),
                auto_paper_trade=bool(self.auto_paper.value),
                record_opportunities_only=(
                    bool(self.record_only.value) or not self.auto_paper.value
                ),
            ),
            quality=DataQualityConfig(
                max_quote_age_ms=int(self.max_age.value),
                max_source_watermark_age_ms=int(self.max_source_age.value),
                max_cross_section_skew_ms=int(self.max_skew.value),
                maximum_missing_weight=float(self.max_missing.value),
                maximum_stale_weight=float(self.max_stale.value),
                maximum_suspended_weight=float(self.max_suspended.value),
                maximum_limit_up_weight=float(self.max_limit_up.value),
                maximum_limit_down_weight=float(self.max_limit_down.value),
                maximum_iopv_error_bps=float(self.max_iopv_error.value),
                kill_switch_enabled=bool(self.kill_switch.value),
            ),
            account=AccountConfig(
                initial_cash=float(self.initial_cash.value),
                initial_etf_inventory=int(self.initial_etf_inventory.value),
                etf_borrow_limit=int(self.etf_borrow_limit.value),
                allow_stock_borrow=bool(self.allow_stock_borrow.value),
            ),
            primary_market=PrimaryMarketConfig(
                rejection_probability=float(
                    self.primary_reject_probability.value
                ),
                cash_component_error=float(self.cash_component_error.value),
                final_cash_delay_ms=int(self.final_cash_delay.value),
            ),
        )

    def _ensure_runtime(self, force: bool = False) -> None:
        path = self.pcf.selected_path
        if path is None:
            raise ValueError("缺少已校验的当日PCF")
        pcf, report = validate_executable_pcf(
            path,
            self.pcf.etf_code,
            self.pcf.trading_date,
        )
        if not report.valid:
            raise ValueError("PCF校验未通过：{}".format(", ".join(report.errors)))
        runtime_pcf = (
            _cross_border_proxy_pcf(pcf) if self.cross_border else pcf
        )
        config = self._build_config()
        signature = json.dumps(
            config.to_dict(include_secrets=True),
            sort_keys=True,
            ensure_ascii=True,
        )
        current_signature = (
            json.dumps(
                self.config.to_dict(include_secrets=True),
                sort_keys=True,
                ensure_ascii=True,
            )
            if self.config is not None
            else None
        )
        if not force and self.engine is not None and signature == current_signature:
            return

        previous_source = self.source
        self.stop_runtime()
        if isinstance(
            previous_source,
            (
                ExecutableRecordingReplayMarketDataSource,
                RedisMarketDataSource,
            ),
        ):
            previous_source.disconnect()
        self.config = config
        self.pcf_document = pcf
        self.pcf_report = report
        self.engine = PaperArbitrageEngine(runtime_pcf, config)

        if config.data_source == DataSourceMode.EXECUTABLE_REPLAY:
            self.source = ExecutableRecordingReplayMarketDataSource(
                config.file_replay.path,
                runtime_pcf,
            )
            first = self.source.first_snapshot
            source_label = "完整五档采集文件回放"
            source_text = (
                "文件={}；首条快照={}；ETF五档={}/{}；"
                "成功记录成分股={}。盘口、时间戳与缺失状态均按采集文件原样回放。"
            ).format(
                self.source.path,
                first.snapshot_timestamp.strftime("%Y-%m-%d %H:%M:%S.%f"),
                len(first.etf_order_book.bids),
                len(first.etf_order_book.asks),
                len(first.component_order_books),
            )
            self._show_recording_summary(self.source)
        elif config.data_source == DataSourceMode.REDIS:
            self.source = RedisMarketDataSource(
                config.redis,
                config.etf_code,
                runtime_pcf,
            )
            source_label = "Redis实时五档纸面模拟"
            source_text = (
                "实时读取交易日Hash中的ETF与PCF成分五档盘口，轮询间隔={}毫秒；"
                "本页面不重复写采集文件。"
            ).format(config.redis.poll_interval_ms)
        else:
            raise ValueError("当前页面不再支持模拟行情或旧版行情文件")

        self.snapshot = None
        self.result = None
        self.history = _new_history()
        self._clear_sources()
        self._refresh_pcf_views()
        self._update_views()
        self.data_source_message.object = (
            '<div class="exec-status">{}</div>'.format(source_text)
        )
        self._set_status(
            "配置已应用，数据源：{}。{}"
            "仅进行纸面模拟，不连接真实交易柜台。".format(
                source_label,
                source_text,
            ),
            "ready",
        )

    def _advance_once(self) -> PaperEngineResult:
        if self.source is None or self.engine is None:
            self._ensure_runtime()
        snapshot = self.source.step()
        fill_snapshot = None
        if (
            self.engine.config.execution.auto_paper_trade
            and not self.engine.config.execution.record_opportunities_only
        ):
            target = snapshot.snapshot_timestamp + timedelta(
                milliseconds=(
                    self.engine.config.execution.decision_latency_ms
                    + self.engine.config.execution.order_latency_ms
                )
            )
            fill_snapshot = self.source.snapshot_at_or_after(target)
        result = self.engine.process(snapshot, fill_snapshot)
        self.snapshot = snapshot
        self.result = result
        _append_history(self.history, snapshot, result)
        self._stream_snapshot(snapshot, result)
        self._update_views()
        return result

    def _apply_config(self, _event) -> None:
        try:
            self._ensure_runtime(force=True)
            self._set_status("配置已应用，运行时已重置。", "ready")
        except Exception as exc:
            self._set_status("应用配置失败：{}".format(exc), "error")

    def _start(self, _event) -> None:
        try:
            self._ensure_runtime()
            self.source.start()
            callback_period = (
                int(self.config.redis.poll_interval_ms)
                if self.config.data_source == DataSourceMode.REDIS
                else 250
            )
            if self.callback is None:
                self.callback = pn.state.add_periodic_callback(
                    self._tick,
                    period=callback_period,
                    start=True,
                )
            elif not self.callback.running:
                self.callback.period = callback_period
                self.callback.start()
            self._set_status("连续采集/模拟/回放中。", "running")
        except Exception as exc:
            self._set_status("启动失败：{}".format(exc), "error")

    def _pause(self, _event) -> None:
        if self.source is not None:
            self.source.stop()
        if self.callback is not None and self.callback.running:
            self.callback.stop()
        self._set_status("已暂停。", "paused")

    def _step(self, _event) -> None:
        try:
            self._ensure_runtime()
            self._advance_once()
            self._set_status("已推进一个快照。", "paused")
        except StopIteration:
            self._set_status("模拟/回放已结束。", "completed")
        except NoNewSnapshotError:
            self._set_status("Redis快照尚未更新。", "paused")
        except Exception as exc:
            self._set_status("单步失败：{}".format(exc), "error")

    def _reset(self, _event) -> None:
        try:
            self._ensure_runtime(force=True)
            self._set_status("运行时已重置。", "ready")
        except Exception as exc:
            self._set_status("重置失败：{}".format(exc), "error")

    def _tick(self) -> None:
        if self.source is None or not self.source.health().running:
            if self.callback is not None and self.callback.running:
                self.callback.stop()
            return
        steps = self._steps_per_refresh()
        try:
            for _ in range(steps):
                self._advance_once()
        except StopIteration:
            self.source.stop()
            if self.callback is not None and self.callback.running:
                self.callback.stop()
            self._set_status("模拟/回放已结束。", "completed")
        except NoNewSnapshotError:
            return
        except Exception as exc:
            self.source.stop()
            if self.callback is not None and self.callback.running:
                self.callback.stop()
            self._set_status("连续运行已停止：{}".format(exc), "error")

    def _steps_per_refresh(self) -> int:
        if self.source_mode.value == DataSourceMode.REDIS:
            return 1
        tick_ms = int(self.file_step_ms.value)
        speed = float(self.file_speed.value)
        replay_tick_seconds = tick_ms / 1000.0 / max(speed, 1e-9)
        return max(1, int(0.25 / replay_tick_seconds + 0.999999))

    def _inspect_recording(self, _event) -> None:
        try:
            path = self.pcf.selected_path
            if path is None:
                raise ValueError("请先选择并校验同交易日PCF")
            pcf, report = validate_executable_pcf(
                path,
                self.pcf.etf_code,
                self.pcf.trading_date,
            )
            if not report.valid:
                raise ValueError(
                    "PCF校验未通过：{}".format(", ".join(report.errors))
                )
            replay = ExecutableRecordingReplayMarketDataSource(
                _resolve_executable_recording_path(
                    self.project_root,
                    self.pcf.trading_date,
                    self.pcf.etf_code,
                    self.recording_path.value.strip(),
                ),
                pcf,
            )
            self._show_recording_summary(replay)
            self._set_file_message(
                "采集文件检查通过，可应用配置后开始回放。",
                "success",
            )
        except Exception as exc:
            self.file_summary = None
            self.file_summary_table.value = pd.DataFrame()
            self._set_file_message(
                "采集文件检查失败：{}".format(exc),
                "error",
            )

    def _show_recording_summary(
        self,
        replay: ExecutableRecordingReplayMarketDataSource,
    ) -> None:
        first = replay.first_snapshot
        self.file_summary = {
            "文件": str(replay.path),
            "大小(MB)": round(replay.total_bytes / 1024 / 1024, 3),
            "首条快照": first.snapshot_timestamp,
            "ETF": first.etf_order_book.symbol,
            "ETF买档": len(first.etf_order_book.bids),
            "ETF卖档": len(first.etf_order_book.asks),
            "成分股快照": len(first.component_order_books),
            "PCF哈希": first.pcf_hash or "",
        }
        self.file_summary_table.value = pd.DataFrame([self.file_summary])

    def _source_mode_changed(self, _event) -> None:
        replay = self.source_mode.value == DataSourceMode.EXECUTABLE_REPLAY
        if hasattr(self, "replay_controls"):
            self.replay_controls.visible = replay
        if hasattr(self, "redis_controls"):
            self.redis_controls.visible = not replay
        message = (
            "使用数据采集页面保存的完整五档JSONL，按时间顺序原样回放。"
            if replay
            else "直接读取Redis完整五档做实时纸面模拟，不重复保存采集文件。"
        )
        self.data_source_message.object = (
            '<div class="exec-status">{}</div>'.format(message)
        )
        if _event is not None:
            self._set_status(
                "{} 请点击“应用配置并重置”或直接开始。".format(message),
                "ready",
            )

    def _pcf_changed(self) -> None:
        previous_source = self.source
        self.stop_runtime()
        if isinstance(
            previous_source,
            (
                ExecutableRecordingReplayMarketDataSource,
                RedisMarketDataSource,
            ),
        ):
            previous_source.disconnect()
        self.source = None
        self.engine = None
        self.config = None
        self._refresh_pcf_views()
        self._set_status("PCF选择已变化，请应用配置。", "ready")

    def _build_price_figure(self):
        chart = figure(
            title="ETF价格、盘口与内部IOPV（有效价格自动缩放）",
            x_axis_type="datetime",
            y_range=DataRange1d(
                range_padding=0.12,
                range_padding_units="percent",
                only_visible=True,
                default_span=0.01,
            ),
            height=380,
            sizing_mode="stretch_width",
            tools="xpan,xwheel_zoom,box_zoom,reset,save",
            active_scroll="xwheel_zoom",
        )
        specs = (
            ("etf_last", "ETF最新价", ETF_COLOR, "solid", 2.2),
            ("etf_bid", "ETF买一价", BID_COLOR, "dotted", 1.4),
            ("etf_ask", "ETF卖一价", ASK_COLOR, "dotted", 1.4),
            ("internal_iopv", "内部IOPV", IOPV_COLOR, "solid", 2.6),
        )
        for field, label, color, dash, width in specs:
            chart.line(
                "timestamp",
                field,
                source=self.market_source,
                legend_label=label,
                color=color,
                line_dash=dash,
                line_width=width,
            )
        chart.add_tools(
            CrosshairTool(dimensions="height"),
            HoverTool(
                tooltips=[
                    ("时间", "@timestamp{%F %T.%3N}"),
                    ("ETF", "@etf_last{0.0000} / @etf_last_rel_bps{0.00} bp"),
                    ("买一", "@etf_bid{0.0000} / @etf_bid_rel_bps{0.00} bp"),
                    ("卖一", "@etf_ask{0.0000} / @etf_ask_rel_bps{0.00} bp"),
                    (
                        "内部IOPV",
                        "@internal_iopv{0.0000} / "
                        "@internal_iopv_rel_bps{0.00} bp",
                    ),
                ],
                formatters={"@timestamp": "datetime"},
                mode="vline",
            ),
        )
        self.opening_span = Span(
            location=0,
            dimension="width",
            line_color="#596368",
            line_dash="dashed",
            line_width=1.2,
            visible=False,
        )
        chart.add_layout(self.opening_span)
        chart.add_layout(
            LinearAxis(
                axis_label="相对开盘价（bp）",
                formatter=CustomJSTickFormatter(
                    args={"opening": self.opening_source},
                    code="""
const reference = opening.data.price[0]
if (reference == null || !isFinite(reference) || reference <= 0) {
  return ""
}
return ((tick / reference - 1) * 10000).toFixed(1)
""",
                ),
            ),
            "right",
        )
        chart.legend.orientation = "horizontal"
        chart.legend.location = "top_left"
        chart.legend.click_policy = "hide"
        chart.yaxis[0].axis_label = "实际价格"
        chart.grid.grid_line_color = "#e4e8ea"
        return chart

    def _build_edge_figure(self):
        chart = figure(
            title="可计算快照的申购/赎回净利润（空白表示深度不足）",
            x_axis_type="datetime",
            height=290,
            sizing_mode="stretch_width",
            tools="xpan,xwheel_zoom,box_zoom,reset,save",
            active_scroll="xwheel_zoom",
        )
        chart.line(
            "timestamp",
            "creation_bps",
            source=self.market_source,
            legend_label="申购净利润（bp）",
            color=POSITIVE,
            line_width=2,
        )
        chart.scatter(
            "timestamp",
            "creation_bps",
            source=self.market_source,
            color=POSITIVE,
            marker="circle",
            size=4,
            alpha=0.75,
        )
        chart.line(
            "timestamp",
            "redemption_bps",
            source=self.market_source,
            legend_label="赎回净利润（bp）",
            color=NEGATIVE,
            line_width=2,
        )
        chart.scatter(
            "timestamp",
            "redemption_bps",
            source=self.market_source,
            color=NEGATIVE,
            marker="circle",
            size=4,
            alpha=0.75,
        )
        chart.add_layout(
            Span(
                location=0,
                dimension="width",
                line_color="#596368",
                line_width=1,
            )
        )
        chart.add_tools(
            CrosshairTool(dimensions="height"),
            HoverTool(
                tooltips=[
                    ("时间", "@timestamp{%F %T.%3N}"),
                    ("申购", "@creation_bps{+0.00} bp"),
                    ("赎回", "@redemption_bps{+0.00} bp"),
                ],
                formatters={"@timestamp": "datetime"},
                mode="vline",
            ),
        )
        chart.yaxis.axis_label = "净利润（bp）"
        chart.legend.orientation = "horizontal"
        chart.legend.location = "top_left"
        chart.grid.grid_line_color = "#e4e8ea"
        return chart

    def _build_pnl_figure(self):
        chart = figure(
            title="累计模拟PnL",
            x_axis_type="datetime",
            height=340,
            sizing_mode="stretch_width",
            tools="xpan,xwheel_zoom,box_zoom,reset,save",
            active_scroll="xwheel_zoom",
        )
        chart.line(
            "timestamp",
            "cumulative_pnl",
            source=self.pnl_source,
            color=ACCENT,
            line_width=2,
        )
        chart.scatter(
            "timestamp",
            "cumulative_pnl",
            source=self.pnl_source,
            color=ACCENT,
            size=6,
        )
        chart.add_tools(
            CrosshairTool(dimensions="height"),
            HoverTool(
                tooltips=[
                    ("时间", "@timestamp{%F %T.%3N}"),
                    ("累计PnL", "@cumulative_pnl{0,0.00}"),
                ],
                formatters={"@timestamp": "datetime"},
                mode="vline",
            ),
        )
        chart.yaxis.axis_label = "PnL"
        chart.grid.grid_line_color = "#e4e8ea"
        return chart

    def _stream_snapshot(self, snapshot, result: PaperEngineResult) -> None:
        evaluation = result.decision_evaluation
        etf = snapshot.etf_order_book
        if self.opening_reference_price is None and _is_valid_price(
            etf.last_price
        ):
            self.opening_reference_price = float(etf.last_price)
            self.opening_source.data = {
                "price": [self.opening_reference_price]
            }
            self.opening_span.location = self.opening_reference_price
            self.opening_span.visible = True
            self.price_figure.title.text = (
                "ETF价格、盘口与内部IOPV（开盘基准 {:.4f}=0 bp，右轴）"
            ).format(self.opening_reference_price)

        reference = self.opening_reference_price
        payload = {
            "timestamp": [snapshot.snapshot_timestamp],
            "etf_last": [_chart_price(etf.last_price)],
            "etf_bid": [_chart_price(etf.best_bid)],
            "etf_ask": [_chart_price(etf.best_ask)],
            "official_iopv": [_chart_price(evaluation.official_iopv)],
            "internal_iopv": [_chart_price(evaluation.internal_iopv)],
            "lower_bound": [_chart_price(evaluation.lower_bound)],
            "upper_bound": [_chart_price(evaluation.upper_bound)],
            "etf_last_rel_bps": [
                _relative_bps(etf.last_price, reference)
            ],
            "etf_bid_rel_bps": [
                _relative_bps(etf.best_bid, reference)
            ],
            "etf_ask_rel_bps": [
                _relative_bps(etf.best_ask, reference)
            ],
            "official_iopv_rel_bps": [
                _relative_bps(evaluation.official_iopv, reference)
            ],
            "internal_iopv_rel_bps": [
                _relative_bps(evaluation.internal_iopv, reference)
            ],
            "lower_bound_rel_bps": [
                _relative_bps(evaluation.lower_bound, reference)
            ],
            "upper_bound_rel_bps": [
                _relative_bps(evaluation.upper_bound, reference)
            ],
            "creation_bps": [
                _chart_number(evaluation.creation.net_profit_bps)
            ],
            "redemption_bps": [
                _chart_number(evaluation.redemption.net_profit_bps)
            ],
        }
        self.market_source.stream(payload, rollover=20_000)
        pnl = pd.DataFrame(self.history["pnl"])
        if not pnl.empty:
            self.pnl_source.data = {
                "timestamp": list(pd.to_datetime(pnl["timestamp"])),
                "cumulative_pnl": list(pnl["final_pnl"].cumsum()),
            }
        elif self.engine is not None:
            self.pnl_source.data = {"timestamp": [], "cumulative_pnl": []}

    def _update_views(self) -> None:
        self._update_runtime_metrics()
        self._update_runtime_status()
        self._update_progress()
        self._refresh_pcf_views()
        self._update_market_tables()
        self._update_history_tables()
        self.config_pane.object = self.config.to_dict() if self.config else {}

    def _update_runtime_metrics(self) -> None:
        if self.snapshot is None or self.result is None:
            self.runtime_metrics.object = _metric_strip(
                [
                    ("系统状态", "未启动", ACCENT),
                    ("新交易许可", "--", BID_COLOR),
                    ("风控熔断", "--", ASK_COLOR),
                    ("ETF Last", "--", ETF_COLOR),
                    ("ETF Bid", "--", BID_COLOR),
                    ("ETF Ask", "--", ASK_COLOR),
                ]
            )
            self.secondary_metrics.object = _metric_strip(
                ([
                    ("官方IOPV", "--", NEGATIVE),
                    ("内部IOPV", "--", IOPV_COLOR),
                    ("套利下界", "--", "#8B5E34"),
                    ("套利上界", "--", "#8B5E34"),
                    ("申购净利润", "--", POSITIVE),
                    ("赎回净利润", "--", NEGATIVE),
                ] + (
                    [("HKD/CNY Bid", "--", BID_COLOR), ("HKD/CNY Ask", "--", ASK_COLOR)]
                    if self.cross_border
                    else []
                ))
            )
            return
        evaluation = self.result.decision_evaluation
        etf = self.snapshot.etf_order_book
        health = self.source.health() if self.source else None
        status = (
            STATUS_LABELS.get(health.status, health.status) if health else "未加载"
        )
        self.runtime_metrics.object = _metric_strip(
            [
                ("系统状态", status, ACCENT),
                (
                    "市场阶段",
                    evaluation.quality.market_phase.value,
                    ACCENT,
                ),
                (
                    "申购许可",
                    "允许" if evaluation.quality.creation_enabled else "禁止",
                    BID_COLOR
                    if evaluation.quality.creation_enabled
                    else ASK_COLOR,
                ),
                (
                    "赎回许可",
                    "允许" if evaluation.quality.redemption_enabled else "禁止",
                    BID_COLOR
                    if evaluation.quality.redemption_enabled
                    else ASK_COLOR,
                ),
                (
                    "风控熔断",
                    "已触发" if evaluation.quality.kill_switch else "未触发",
                    ASK_COLOR if evaluation.quality.kill_switch else BID_COLOR,
                ),
                ("ETF Last", _price(etf.last_price), ETF_COLOR),
                ("ETF Bid", _price(etf.best_bid), BID_COLOR),
                ("ETF Ask", _price(etf.best_ask), ASK_COLOR),
            ]
        )
        fx_quote = self.snapshot.hkd_cny_quote
        self.secondary_metrics.object = _metric_strip(
            ([
                ("官方IOPV", _price(evaluation.official_iopv), NEGATIVE),
                ("内部IOPV", _price(evaluation.internal_iopv), IOPV_COLOR),
                ("套利下界", _price(evaluation.lower_bound), "#8B5E34"),
                ("套利上界", _price(evaluation.upper_bound), "#8B5E34"),
                (
                    "申购净利润",
                    _profit(evaluation.creation),
                    POSITIVE,
                ),
                (
                    "赎回净利润",
                    _profit(evaluation.redemption),
                    NEGATIVE,
                ),
            ] + (
                [
                    ("HKD/CNY Bid", _fx_price(fx_quote.bid if fx_quote else None), BID_COLOR),
                    ("HKD/CNY Ask", _fx_price(fx_quote.ask if fx_quote else None), ASK_COLOR),
                ]
                if self.cross_border
                else []
            ))
        )

    def _update_runtime_status(self) -> None:
        if self.runtime_status.object:
            return
        source_label = {
            DataSourceMode.EXECUTABLE_REPLAY: "完整五档采集文件回放",
            DataSourceMode.REDIS: "Redis实时五档纸面模拟",
        }.get(self.source_mode.value, str(self.source_mode.value))
        self.runtime_status.object = (
            '<div class="exec-status">状态：就绪　数据源：{}　'
            "仅进行纸面模拟，不连接真实交易柜台。</div>"
        ).format(source_label)

    def _update_progress(self) -> None:
        if isinstance(
            self.source,
            ExecutableRecordingReplayMarketDataSource,
        ):
            self.progress.value = self.source.progress_percent
            self.progress.name = (
                "完整五档回放：已处理{}条，预读{}条，文件{}%"
            ).format(
                self.source.records_read,
                self.source.buffered_records,
                self.source.progress_percent,
            )
        else:
            self.progress.value = 0
            self.progress.name = "实时数据源不显示固定进度"

    def _refresh_pcf_views(self) -> None:
        path = self.pcf.selected_path
        if path is None:
            self.pcf_header_table.value = pd.DataFrame()
            self.pcf_components_table.value = pd.DataFrame()
            return
        try:
            document, report = validate_executable_pcf(
                path,
                self.pcf.etf_code,
                self.pcf.trading_date,
            )
        except Exception:
            self.pcf_header_table.value = pd.DataFrame()
            self.pcf_components_table.value = pd.DataFrame()
            return
        self.pcf_document = document
        self.pcf_report = report
        self.pcf_header_table.value = pd.DataFrame(
            [
                {
                    "ETF代码": document.etf_code,
                    "名称": document.symbol,
                    "交易日": document.trading_day,
                    "最小申赎单位": document.creation_redemption_unit,
                    "成分股数量": len(document.components),
                    "预估现金差额": document.estimate_cash_component,
                    "最大现金替代比例": document.max_cash_ratio,
                    "允许申购": document.creation_allowed,
                    "允许赎回": document.redemption_allowed,
                    "禁止现金替代": report.prohibited_count,
                    "允许现金替代": report.optional_count,
                    "必须现金替代": report.mandatory_count,
                    "校验状态": "通过" if report.valid else "失败",
                }
            ]
        )
        self.pcf_components_table.value = pd.DataFrame(
            [
                {
                    "证券代码": item.stock_code,
                    "名称": item.symbol,
                    "数量": item.component_share,
                    "替代标志": item.substitute_flag.name,
                    "申购替代金额": item.creation_cash_substitute,
                    "赎回替代金额": item.redemption_cash_substitute,
                    "申购溢价率": item.premium_ratio,
                    "赎回折价率": item.discount_ratio,
                }
                for item in document.components
            ]
        )

    def _update_market_tables(self) -> None:
        if self.snapshot is None or self.result is None:
            self.etf_book_table.value = pd.DataFrame()
            self.component_books_table.value = pd.DataFrame()
            self.component_plan_table.value = pd.DataFrame()
            self.basket_table.value = pd.DataFrame()
            self.capacity_table.value = pd.DataFrame()
        else:
            evaluation = self.result.decision_evaluation
            self.etf_book_table.value = _book_rows(
                self.snapshot.etf_order_book
            )
            self.component_books_table.value = _component_book_rows(
                self.snapshot
            )
            self.component_plan_table.value = _component_plan_rows(
                evaluation
            )
            self.basket_table.value = pd.DataFrame(
                [
                    {
                        "方向": "申购",
                        "实物篮子": evaluation.creation.basket.physical_value,
                        "现金替代": evaluation.creation.basket.substitution_cash,
                        "预估现金差额": (
                            evaluation.creation.basket.estimate_cash_component
                        ),
                        "篮子合计": evaluation.creation.basket.total_value,
                        "完整成交": evaluation.creation.basket.fully_filled,
                        "自适应替代证券": ",".join(
                            evaluation.creation.basket.adaptive_cash_substituted_symbols
                        ),
                        "现金替代比例": evaluation.creation.basket.optional_cash_ratio,
                        "PCF替代上限": evaluation.creation.basket.max_cash_ratio,
                        "瓶颈证券": ",".join(
                            evaluation.creation.basket.bottleneck_symbols
                        ),
                        "失败原因": ",".join(
                            evaluation.creation.basket.failure_reasons
                        ),
                    },
                    {
                        "方向": "赎回",
                        "实物篮子": evaluation.redemption.basket.physical_value,
                        "现金替代": evaluation.redemption.basket.substitution_cash,
                        "预估现金差额": (
                            evaluation.redemption.basket.estimate_cash_component
                        ),
                        "篮子合计": evaluation.redemption.basket.total_value,
                        "完整成交": evaluation.redemption.basket.fully_filled,
                        "自适应替代证券": ",".join(
                            evaluation.redemption.basket.adaptive_cash_substituted_symbols
                        ),
                        "现金替代比例": evaluation.redemption.basket.optional_cash_ratio,
                        "PCF替代上限": evaluation.redemption.basket.max_cash_ratio,
                        "瓶颈证券": ",".join(
                            evaluation.redemption.basket.bottleneck_symbols
                        ),
                        "失败原因": ",".join(
                            evaluation.redemption.basket.failure_reasons
                        ),
                    },
                ]
            )
            self.capacity_table.value = pd.DataFrame(
                [
                    asdict(evaluation.creation_capacity),
                    asdict(evaluation.redemption_capacity),
                ]
            )
        if self.source is not None:
            self.health_table.value = pd.DataFrame(
                [asdict(self.source.health())]
            )
        else:
            self.health_table.value = pd.DataFrame()

    def _update_history_tables(self) -> None:
        self.opportunities_table.value = pd.DataFrame(
            self.history["opportunities"][-200:]
        )
        self.orders_table.value = pd.DataFrame(self.history["orders"])
        self.fills_table.value = pd.DataFrame(self.history["fills"])
        self.primary_table.value = pd.DataFrame(
            self.history["primary_market_requests"]
        )
        pnl = pd.DataFrame(self.history["pnl"])
        if not pnl.empty:
            pnl["累计PnL"] = pnl["final_pnl"].cumsum()
        self.pnl_table.value = pnl
        self.quality_table.value = pd.DataFrame(
            self.history["data_quality_events"][-200:]
        )
        self._update_funnel()
        selected = self.export_select.value
        self.history_table.value = pd.DataFrame(self.history[selected])
        self.export_csv.filename = "{}.csv".format(selected)
        self.export_parquet.filename = "{}.parquet".format(selected)

    def _update_funnel(self) -> None:
        rows = pd.DataFrame(self.history["opportunities"])
        snapshots = len(self.history["snapshots"])
        if rows.empty:
            counts = [snapshots, 0, 0, 0, 0, 0]
        else:
            n1 = int((rows.groupby("timestamp")["net_profit"].max() > 0).sum())
            depth_ok = rows["basket_fully_filled"] & rows["etf_fully_filled"]
            n2 = int(rows.loc[depth_ok, "timestamp"].nunique())
            n3 = int(rows[rows["executable"]]["timestamp"].nunique())
            n4 = len(
                [item for item in self.history["trades"] if item["fill_time"]]
            )
            n5 = len(
                [item for item in self.history["trades"] if item["final_pnl"] > 0]
            )
            counts = [snapshots, n1, n2, n3, n4, n5]
        self.funnel_table.value = pd.DataFrame(
            {
                "阶段": [
                    "N0快照",
                    "N1成本前正边际",
                    "N2深度完整",
                    "N3成本后可执行",
                    "N4延迟后成交",
                    "N5最终盈利",
                ],
                "数量": counts,
            }
        )

    def _clear_sources(self) -> None:
        self.opening_reference_price = None
        self.opening_source.data = {"price": [None]}
        self.opening_span.location = 0
        self.opening_span.visible = False
        self.price_figure.title.text = (
            "ETF价格、盘口与内部IOPV（有效价格自动缩放）"
        )
        self.market_source.data = {
            "timestamp": [],
            "etf_last": [],
            "etf_bid": [],
            "etf_ask": [],
            "official_iopv": [],
            "internal_iopv": [],
            "lower_bound": [],
            "upper_bound": [],
            "etf_last_rel_bps": [],
            "etf_bid_rel_bps": [],
            "etf_ask_rel_bps": [],
            "official_iopv_rel_bps": [],
            "internal_iopv_rel_bps": [],
            "lower_bound_rel_bps": [],
            "upper_bound_rel_bps": [],
            "creation_bps": [],
            "redemption_bps": [],
        }
        self.pnl_source.data = {"timestamp": [], "cumulative_pnl": []}
        self.progress.value = 0

    def _set_status(self, text: str, state: str) -> None:
        colors = {
            "ready": ("#15616D", "#f4f7f7"),
            "running": ("#2F6B4F", "#eef7f2"),
            "paused": ("#8B5E34", "#fff8ec"),
            "completed": ("#3A6EA5", "#eef4fb"),
            "error": ("#A33A2B", "#fff1ef"),
        }
        foreground, background = colors.get(
            state, ("#374147", "#f4f7f7")
        )
        self.runtime_status.object = (
            '<div class="exec-status" style="border-left-color:{fg};'
            'background:{bg};color:{fg}">{text}</div>'
        ).format(fg=foreground, bg=background, text=text)

    def _set_file_message(self, text: str, kind: str) -> None:
        colors = {
            "success": ("#2F6B4F", "#eef7f2"),
            "error": ("#A33A2B", "#fff1ef"),
        }
        foreground, background = colors.get(
            kind, ("#374147", "#f4f7f7")
        )
        self.file_message.object = (
            '<div style="border-left:3px solid {fg};background:{bg};'
            'padding:8px 10px;color:{fg}">{text}</div>'
        ).format(fg=foreground, bg=background, text=text)

    def _export_table_changed(self, event) -> None:
        self.history_table.value = pd.DataFrame(self.history[event.new])
        self.export_csv.filename = "{}.csv".format(event.new)
        self.export_parquet.filename = "{}.parquet".format(event.new)

    def _download_csv(self):
        frame = pd.DataFrame(self.history[self.export_select.value])
        return BytesIO(frame.to_csv(index=False).encode("utf-8-sig"))

    def _download_parquet(self):
        frame = pd.DataFrame(self.history[self.export_select.value])
        buffer = BytesIO()
        frame.to_parquet(buffer, index=False)
        buffer.seek(0)
        return buffer

    def _download_config(self):
        payload = self.config.to_dict() if self.config else self._build_config().to_dict()
        return BytesIO(
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        )

    def _save_run(self, _event) -> None:
        if self.config is None or self.engine is None:
            self.save_message.object = (
                '<div style="color:#A33A2B">尚无可保存的运行时。</div>'
            )
            return
        try:
            recorder = RunRecorder(self.run_root, self.config.to_dict())
            for name, rows in self.history.items():
                for row in rows:
                    recorder.record(name, row)
            output = recorder.save(
                {
                    "etf_code": self.config.etf_code,
                    "pcf_path": str(self.pcf.selected_path),
                    "final_cash": self.engine.account.cash,
                    "realized_pnl": self.engine.account.realized_pnl,
                }
            )
            self.save_message.object = (
                '<div style="color:#2F6B4F">运行数据已保存：{}</div>'
            ).format(output.relative_to(self.project_root))
        except Exception as exc:
            self.save_message.object = (
                '<div style="color:#A33A2B">保存失败：{}</div>'
            ).format(exc)


def _new_history() -> Dict[str, list[dict]]:
    return {name: [] for name in HISTORY_TABLES}


def _cross_border_proxy_pcf(pcf):
    """Remove the virtual subscription-cash record from proxy valuation.

    The 159900 row is a settlement prepayment record, not an investable basket
    constituent.  Keeping it in the generic simulator would add it to NAV a
    second time and materially overstate the synthetic IOPV.
    """

    components = tuple(cross_border_hk_components(pcf))
    if not components:
        raise ValueError("跨境纸面模拟未识别到HKEX参考成分")
    return replace(
        pcf,
        components=components,
        total_record_num=len(components),
    )


def _snapshot_record(snapshot, evaluation) -> dict:
    etf_payload = {
        "timestamp": snapshot.etf_order_book.exchange_timestamp.isoformat(),
        "status": snapshot.etf_order_book.trading_status.value,
        "instrument_state": snapshot.etf_order_book.instrument_state.value,
        "raw_status": snapshot.etf_order_book.raw_status,
        "bids": [
            [level.price, level.quantity]
            for level in snapshot.etf_order_book.bids
        ],
        "asks": [
            [level.price, level.quantity]
            for level in snapshot.etf_order_book.asks
        ],
    }
    component_payload = {
        symbol: {
            "timestamp": book.exchange_timestamp.isoformat(),
            "status": book.trading_status.value,
            "instrument_state": book.instrument_state.value,
            "state_confidence": book.state_confidence.value,
            "state_reasons": list(book.state_reasons),
            "raw_status": book.raw_status,
            "bids": [[level.price, level.quantity] for level in book.bids],
            "asks": [[level.price, level.quantity] for level in book.asks],
        }
        for symbol, book in snapshot.component_order_books.items()
    }
    fx_quote = snapshot.hkd_cny_quote
    return {
        "timestamp": snapshot.snapshot_timestamp.isoformat(),
        "source_mode": snapshot.source_mode,
        "market_phase": snapshot.market_phase.value,
        "feed_health": snapshot.feed_health.value,
        "source_watermark_age_ms": snapshot.source_watermark_age_ms,
        "etf_code": snapshot.etf_order_book.symbol,
        "etf_last": snapshot.etf_order_book.last_price,
        "etf_bid": snapshot.etf_order_book.best_bid,
        "etf_ask": snapshot.etf_order_book.best_ask,
        "etf_book_json": json.dumps(etf_payload, ensure_ascii=False),
        "official_iopv": snapshot.official_iopv,
        "internal_iopv": evaluation.internal_iopv,
        "hkd_cny_bid": fx_quote.bid if fx_quote else None,
        "hkd_cny_ask": fx_quote.ask if fx_quote else None,
        "hkd_cny_exchange_timestamp": (
            fx_quote.exchange_timestamp.isoformat()
            if fx_quote and fx_quote.exchange_timestamp
            else None
        ),
        "hkd_cny_receive_timestamp": (
            fx_quote.receive_timestamp.isoformat()
            if fx_quote and fx_quote.receive_timestamp
            else None
        ),
        "hkd_cny_source": fx_quote.source if fx_quote else None,
        "sequence_gap": snapshot.sequence_gap,
        "decode_error": snapshot.decode_error,
        "component_books_json": json.dumps(
            component_payload, ensure_ascii=False
        ),
    }


def _opportunity_record(evaluation, result) -> dict:
    return {
        "timestamp": evaluation.timestamp.isoformat(),
        "etf_code": evaluation.etf_code,
        "direction": result.direction.value,
        "cu_count": result.cu_count,
        "gross_profit": _finite_number(result.gross_profit),
        "estimated_costs": _finite_number(result.estimated_costs),
        "safety_buffer": _finite_number(result.safety_buffer),
        "net_profit": _finite_number(result.net_profit),
        "net_profit_bps": _finite_number(result.net_profit_bps),
        "pricing_complete": result.pricing_complete,
        "executable": result.executable,
        "rejection_reasons": ",".join(result.rejection_reasons),
        "basket_value": result.basket.total_value,
        "basket_fully_filled": result.basket.fully_filled,
        "adaptive_cash_symbols": ",".join(
            result.basket.adaptive_cash_substituted_symbols
        ),
        "optional_cash_ratio": result.basket.optional_cash_ratio,
        "max_cash_ratio": result.basket.max_cash_ratio,
        "basket_bottlenecks": ",".join(result.basket.bottleneck_symbols),
        "basket_failure_reasons": ",".join(result.basket.failure_reasons),
        "component_plan_json": json.dumps(
            [
                {
                    "symbol": plan.symbol,
                    "name": plan.name,
                    "action": plan.action.value,
                    "reason": plan.reason,
                    "required_quantity": plan.required_quantity,
                    "visible_quantity": plan.visible_quantity,
                    "filled_quantity": plan.filled_quantity,
                    "unfilled_quantity": plan.unfilled_quantity,
                    "reference_price": plan.reference_price,
                    "cash_amount": plan.cash_amount,
                    "cash_reference_value": plan.cash_reference_value,
                    "substitute_flag": plan.substitute_flag.name,
                    "instrument_state": plan.instrument_state,
                    "state_confidence": plan.state_confidence,
                }
                for plan in result.basket.component_plans.values()
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "etf_fully_filled": result.etf_sweep.fully_filled,
    }


def _append_history(
    history: Dict[str, list[dict]],
    snapshot,
    result: PaperEngineResult,
) -> None:
    evaluation = result.decision_evaluation
    history["snapshots"].append(_snapshot_record(snapshot, evaluation))
    for direction in (evaluation.creation, evaluation.redemption):
        row = _opportunity_record(evaluation, direction)
        history["opportunities"].append(row)
        if not direction.executable:
            history["rejected_opportunities"].append(row)
    history["data_quality_events"].append(
        dict(
            timestamp=evaluation.timestamp.isoformat(),
            **evaluation.quality.to_dict(),
        )
    )
    if result.cycle is None:
        return
    cycle = result.cycle
    history["trades"].append(
        {
            "cycle_id": cycle.cycle_id,
            "direction": cycle.direction,
            "decision_time": cycle.decision_time.isoformat(),
            "fill_time": cycle.fill_time.isoformat() if cycle.fill_time else None,
            "state": cycle.state.value,
            "state_history": ",".join(cycle.state_history),
            "snapshot_profit": cycle.snapshot_profit,
            "execution_profit": cycle.execution_profit,
            "final_pnl": cycle.final_pnl,
            "rejection_reason": cycle.rejection_reason,
            "execution_risk_label": cycle.execution_risk_label,
        }
    )
    for order in cycle.orders:
        row = asdict(order)
        row["submit_time"] = order.submit_time.isoformat()
        history["orders"].append(row)
    for fill in cycle.fills:
        row = asdict(fill)
        row["fill_time"] = fill.fill_time.isoformat()
        history["fills"].append(row)
    history["pnl"].append(
        {
            "timestamp": (cycle.fill_time or cycle.decision_time).isoformat(),
            "cycle_id": cycle.cycle_id,
            "snapshot_pnl": cycle.snapshot_profit,
            "execution_pnl": cycle.execution_profit,
            "final_pnl": cycle.final_pnl,
        }
    )
    if result.primary_request is not None:
        request = result.primary_request
        row = asdict(request)
        row["status"] = request.status.value
        row["submit_time"] = request.submit_time.isoformat()
        row["confirm_time"] = (
            request.confirm_time.isoformat() if request.confirm_time else None
        )
        row["final_settlement_time"] = (
            request.final_settlement_time.isoformat()
            if request.final_settlement_time
            else None
        )
        row["pcf_date"] = request.pcf_date.isoformat()
        row["status_history"] = ",".join(request.status_history)
        history["primary_market_requests"].append(row)


def _book_rows(book) -> pd.DataFrame:
    rows = []
    maximum = max(len(book.bids), len(book.asks), 1)
    for index in range(maximum):
        bid = book.bids[index] if index < len(book.bids) else None
        ask = book.asks[index] if index < len(book.asks) else None
        rows.append(
            {
                "档位": index + 1,
                "买量": bid.quantity if bid else None,
                "买价": bid.price if bid else None,
                "卖价": ask.price if ask else None,
                "卖量": ask.quantity if ask else None,
            }
        )
    return pd.DataFrame(rows)


def _resolve_executable_recording_path(
    project_root: Path,
    trading_date,
    etf_code: str,
    configured_path: str,
) -> Path:
    if configured_path:
        path = Path(configured_path).expanduser()
        return path.resolve() if path.is_absolute() else (project_root / path).resolve()
    return (
        project_root
        / "tmp"
        / "executable_recordings"
        / trading_date.strftime("%Y%m%d")
        / "{}.jsonl".format(str(etf_code).zfill(6))
    ).resolve()


def _component_book_rows(snapshot) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "证券代码": symbol,
                "币种": "HKD" if str(book.exchange).upper() == "HKEX" else "CNY",
                "状态": book.instrument_state.value,
                "置信度": book.state_confidence.value,
                "原始状态": book.raw_status,
                "状态证据": ",".join(book.state_reasons),
                "最新价": book.last_price,
                "昨收": book.previous_close,
                "涨停价": book.upper_limit_price,
                "跌停价": book.lower_limit_price,
                "买一": book.best_bid,
                "买一量": book.bids[0].quantity if book.bids else None,
                "卖一": book.best_ask,
                "卖一量": book.asks[0].quantity if book.asks else None,
                "行情时间": book.exchange_timestamp,
                "未更新毫秒": book.quote_inactivity_age_ms,
                "序号": book.sequence_number,
            }
            for symbol, book in snapshot.component_order_books.items()
        ]
    )


def _component_plan_rows(evaluation) -> pd.DataFrame:
    rows = []
    action_priority = {
        "BLOCKED": 0,
        "CASH_ADAPTIVE": 1,
        "CASH_MANDATORY": 2,
        "PHYSICAL": 3,
        "NO_ACTION": 4,
    }
    action_labels = {
        "CASH_MANDATORY": "PCF强制现金替代",
        "CASH_ADAPTIVE": "异常成分自适应现金替代",
        "BLOCKED": "阻断",
        "NO_ACTION": "无需操作",
    }
    reason_labels = {
        "BOOK_DEPTH_FILLED": "五档深度可完整成交",
        "PCF_MANDATORY_CASH_SUBSTITUTION": "PCF规定必须现金替代",
        "LIMIT_UP_LOCKED": "涨停封板，当前无卖盘",
        "LIMIT_DOWN_LOCKED": "跌停封板，当前无买盘",
        "SUSPENDED_CONFIRMED": "已确认停牌",
        "SUSPENDED_SUSPECTED": "疑似停牌，等待更强证据",
        "ONE_SIDED_UNKNOWN": "单边盘口且尚不能确认停板",
        "INSUFFICIENT_DEPTH": "五档可成交数量不足",
        "EMPTY_BOOK": "所需方向没有挂单",
        "MISSING_COMPONENT_BOOK": "缺少该成分行情",
        "MISSING_HKD_CNY_QUOTE": "缺少港币人民币汇率",
        "MISSING_CASH_SUBSTITUTION_REFERENCE": "缺少现金替代估值参考",
        "ZERO_COMPONENT_QUANTITY": "PCF成分数量为零",
    }
    for direction_label, result in (
        ("申购", evaluation.creation),
        ("赎回", evaluation.redemption),
    ):
        for plan in result.basket.component_plans.values():
            action = plan.action.value
            physical_label = "买入成分" if direction_label == "申购" else "卖出成分"
            rows.append(
                {
                    "_priority": action_priority.get(action, 99),
                    "方向": direction_label,
                    "证券代码": plan.symbol,
                    "名称": plan.name,
                    "市场状态": plan.instrument_state,
                    "状态置信度": plan.state_confidence,
                    "PCF替代标志": plan.substitute_flag.name,
                    "执行动作": action_labels.get(action, physical_label),
                    "动作说明": reason_labels.get(plan.reason, plan.reason),
                    "原因代码": plan.reason,
                    "所需数量": plan.required_quantity,
                    "五档挂单量": plan.visible_quantity,
                    "压力后成交量": plan.filled_quantity,
                    "未成交量": plan.unfilled_quantity,
                    "参考价": plan.reference_price,
                    "实物成交额": plan.physical_value,
                    "现金替代额": plan.cash_amount,
                    "替代比例计量市值": plan.cash_reference_value,
                }
            )
    if not rows:
        return pd.DataFrame()
    return (
        pd.DataFrame(rows)
        .sort_values(["_priority", "方向", "证券代码"], kind="stable")
        .drop(columns=["_priority"])
        .reset_index(drop=True)
    )


def _table(height: int):
    return pn.widgets.Tabulator(
        pd.DataFrame(),
        show_index=False,
        disabled=True,
        height=height,
        pagination="remote" if height >= 300 else None,
        page_size=20,
        sizing_mode="stretch_width",
    )


def _int_input(
    label: str,
    value: int,
    start: int,
    end: int,
    step: int,
):
    return pn.widgets.IntInput(
        label=label,
        value=value,
        start=start,
        end=end,
        step=step,
    )


def _float_input(
    label: str,
    value: float,
    start: float,
    end: float,
    step: float,
):
    return pn.widgets.FloatInput(
        label=label,
        value=value,
        start=start,
        end=end,
        step=step,
    )


def _metric_strip(items: list[tuple[str, str, str]]) -> str:
    cells = []
    for label, value, accent in items:
        cells.append(
            '<div class="exec-metric" style="--metric-accent:{accent}">'
            '<div class="exec-label">{label}</div>'
            '<div class="exec-value">{value}</div>'
            "</div>".format(label=label, value=value, accent=accent)
        )
    return '<div class="exec-metrics">{}</div>'.format("".join(cells))


def _price(value: Optional[float]) -> str:
    if value is None or pd.isna(value):
        return "--"
    return "{:.4f}".format(float(value))


def _profit(result) -> str:
    if not result.pricing_complete:
        if "MAX_CASH_SUBSTITUTION_RATIO_EXCEEDED" in result.basket.failure_reasons:
            return "不可计算（现金替代超限）"
        return "不可计算（深度不足）"
    value = _finite_number(result.net_profit)
    return "--" if value is None else "{:,.2f}".format(value)


def _fx_price(value: Optional[float]) -> str:
    if value is None or pd.isna(value):
        return "--"
    return "{:.6f}".format(float(value))


def _is_valid_price(value: Optional[float]) -> bool:
    if value is None:
        return False
    try:
        price = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(price) and price > 0


def _relative_bps(
    value: Optional[float],
    reference: Optional[float],
) -> Optional[float]:
    if not _is_valid_price(value) or not _is_valid_price(reference):
        return None
    return (float(value) / float(reference) - 1.0) * 10_000.0


def _chart_price(value: Optional[float]) -> Optional[float]:
    return float(value) if _is_valid_price(value) else float("nan")


def _chart_number(value: Optional[float]) -> float:
    number = _finite_number(value)
    return number if number is not None else float("nan")


def _finite_number(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None
