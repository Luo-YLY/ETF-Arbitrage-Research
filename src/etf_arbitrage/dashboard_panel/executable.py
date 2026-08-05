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
    SZRedisQuotationClient,
    SZRedisSettings,
    load_pcf_price_seed,
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
    SimulationConfig,
    SimulationPriceSeedMode,
    SimulationScenario,
)
from etf_arbitrage.market_data import (
    DynamicMarketDataLoader,
    FileReplayMarketDataSource,
    MarketDataSource,
    RecordedHistoryMarketDataSource,
    RedisMarketDataSource,
    SimulatedMarketDataSource,
    cross_border_hk_components,
)
from etf_arbitrage.reporting import RunRecorder

from .pcf_controls import PanelPCFControls


ACCENT = "#15616D"
POSITIVE = "#C44536"
NEGATIVE = "#3A6EA5"
ETF_COLOR = "#202A2E"
IOPV_COLOR = "#7A5C9E"
BID_COLOR = "#2F6B4F"
ASK_COLOR = "#C44536"

SCENARIO_LABELS = {
    SimulationScenario.NORMAL: "正常行情",
    SimulationScenario.PREMIUM_SHOCK: "ETF溢价冲击",
    SimulationScenario.DISCOUNT_SHOCK: "ETF折价冲击",
    SimulationScenario.MEAN_REVERSION: "折溢价均值回复",
    SimulationScenario.ETF_DEPTH_SHORTAGE: "ETF盘口深度不足",
    SimulationScenario.COMPONENT_DEPTH_SHORTAGE: "成分股盘口深度不足",
    SimulationScenario.STALE_QUOTE: "成分股报价陈旧",
    SimulationScenario.MISSING_QUOTE: "成分股行情缺失",
    SimulationScenario.SUSPENSION: "成分股停牌",
    SimulationScenario.LIMIT_UP_NO_ASK: "涨停且无卖盘",
    SimulationScenario.LIMIT_DOWN_NO_BID: "跌停且无买盘",
    SimulationScenario.DECODE_ERROR: "行情解码错误",
    SimulationScenario.SEQUENCE_GAP: "行情序号断档",
    SimulationScenario.CROSSED_BOOK: "异常交叉盘口",
    SimulationScenario.PCF_INVALID: "PCF交易日无效",
}

STATUS_LABELS = {
    "RUNNING": "运行中",
    "READY": "就绪",
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
        self.source: Optional[MarketDataSource] = None
        self.file_source: Optional[FileReplayMarketDataSource] = None
        self.file_summary = None
        self.engine: Optional[PaperArbitrageEngine] = None
        self.snapshot = None
        self.result: Optional[PaperEngineResult] = None
        self.config: Optional[PaperArbitrageConfig] = None
        self.pcf_document = None
        self.pcf_report = None
        self.redis_price_seed = None
        self.opening_reference_price: Optional[float] = None
        self.callback = None
        self.history = _new_history()

        self._build_data_widgets()
        self._build_execution_widgets()
        self._build_runtime_widgets()
        self._build_output_models()
        self._wire_events()
        self._source_mode_changed(None)
        self._price_seed_mode_changed(None)
        if self.cross_border:
            self.scenario.value = SimulationScenario.MEAN_REVERSION
            self.optional_cash.value = False
            self.optional_cash.disabled = True
            self.optional_cash.name = "跨境代理篮子使用方向性模拟盘口（固定）"
        self._refresh_pcf_views()
        self._update_views()

    def _build_data_widgets(self) -> None:
        self.source_mode = pn.widgets.Select(
            label="行情数据源",
            options={
                "模拟行情（本地/合成）": DataSourceMode.SIMULATED,
                "上传/读取历史行情": DataSourceMode.FILE_REPLAY,
                "标准化Redis实时行情": DataSourceMode.REDIS,
            },
            value=DataSourceMode.SIMULATED,
        )
        self.scenario = pn.widgets.Select(
            label="模拟场景",
            options={
                label: scenario for scenario, label in SCENARIO_LABELS.items()
            },
            value=SimulationScenario.NORMAL,
        )
        self.random_seed = _int_input("随机种子", 42, 0, 1_000_000, 1)
        price_seed_options = {
            "本地历史数据（逐条回放）": (
                SimulationPriceSeedMode.AUTO_LOCAL
            ),
            "指定本地历史文件": (
                SimulationPriceSeedMode.LOCAL_RECORDING
            ),
            "完全模拟数据": SimulationPriceSeedMode.SYNTHETIC,
            "内网Redis实时最新价": SimulationPriceSeedMode.REDIS_LATEST,
        }
        self.price_seed_mode = pn.widgets.Select(
            label="模拟价格来源",
            options=price_seed_options,
            value=SimulationPriceSeedMode.AUTO_LOCAL,
        )
        self.local_recording_path = pn.widgets.TextInput(
            label="本地采集文件（可选）",
            value="",
            placeholder="留空自动读取 tmp/recordings/交易日/ETF代码.jsonl",
        )
        self.redis_seed_suffix = pn.widgets.TextInput(
            label="Redis代码后缀",
            value=".SZ",
            placeholder=".SZ",
        )
        self.redis_hkd_cny_code = pn.widgets.TextInput(
            label="Redis HKD/CNY代码（可选）",
            value=os.getenv("SZ_REDIS_HKD_CNY_CODE", ""),
            placeholder="留空时使用下方模拟汇率",
        )
        self.hkd_cny_mid = _float_input(
            "模拟HKD/CNY中间价", 0.92, 0.01, 5.0, 0.0001
        )
        self.hkd_cny_spread_bps = _float_input(
            "模拟汇率价差（bp）", 2.0, 0.0, 1000.0, 0.1
        )
        self.hkd_cny_volatility = _float_input(
            "模拟汇率单Tick波动率", 0.0, 0.0, 0.02, 0.00001
        )
        self.price_seed_message = pn.pane.HTML(
            '<div class="exec-status">价格基准：自动优先读取PCF交易日对应的本地采集文件；不存在时使用完全模拟。买卖盘深度与后续路径仍由模拟器生成。</div>',
            sizing_mode="stretch_width",
        )
        self.tick_ms = _int_input("Tick间隔（毫秒）", 1000, 10, 60_000, 10)
        self.total_ticks = _int_input(
            "模拟时间轴长度（Tick）", 300, 10, 100_000, 10
        )
        self.simulation_speed = pn.widgets.Select(
            label="模拟加速",
            options={
                "{}x".format(value): value
                for value in (0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0)
            },
            value=1.0,
        )
        self.premium_shock = _float_input(
            "冲击（bp）", 35.0, 0.0, 500.0, 1.0
        )
        self.base_volatility = _float_input(
            "基础波动率", 0.00015, 0.0, 0.02, 0.00005
        )
        self.book_levels = _int_input("盘口档位", 5, 1, 10, 1)
        self.depth_per_level = _float_input(
            "每档深度（CU倍数）", 2.0, 0.01, 20.0, 0.1
        )
        self.quote_latency = _int_input(
            "行情延迟（毫秒）", 50, 0, 60_000, 10
        )
        self.stale_ratio = _float_input(
            "陈旧报价比例", 0.10, 0.0, 1.0, 0.01
        )
        self.missing_ratio = _float_input(
            "缺失报价比例", 0.05, 0.0, 1.0, 0.01
        )
        self.etf_spread = _float_input(
            "ETF价差（bp）", 2.0, 0.0, 1000.0, 0.5
        )
        self.component_spread = _float_input(
            "成分股价差（bp）", 4.0, 0.0, 1000.0, 0.5
        )
        self.depth_decay = _float_input(
            "盘口深度衰减", 0.85, 0.01, 1.0, 0.01
        )
        self.shock_start_tick = _int_input(
            "冲击开始Tick", 0, 0, 1_000_000, 1
        )
        self.shock_duration = _int_input(
            "冲击持续Tick", 30, 1, 1_000_000, 1
        )
        self.mean_reversion_speed = _float_input(
            "均值回复速度", 0.12, 0.0, 10.0, 0.01
        )
        self.suspended_weight = _float_input(
            "模拟停牌比例", 0.05, 0.0, 1.0, 0.01
        )
        self.limit_up_weight = _float_input(
            "模拟涨停比例", 0.05, 0.0, 1.0, 0.01
        )
        self.limit_down_weight = _float_input(
            "模拟跌停比例", 0.05, 0.0, 1.0, 0.01
        )
        self.sequence_gap_probability = _float_input(
            "序号断档概率", 0.0, 0.0, 1.0, 0.01
        )

        self.market_upload = pn.widgets.FileInput(
            label="上传历史行情",
            accept=".csv,.json,.jsonl,.parquet",
            multiple=False,
        )
        self.market_path = pn.widgets.TextInput(
            label="本地文件/文件夹/通配符",
            placeholder="支持CSV、JSON、JSONL、Parquet",
        )
        self.schema_mapping = pn.widgets.TextAreaInput(
            label="Schema映射JSON",
            value="{}",
            height=100,
        )
        self.file_speed = _float_input(
            "文件回放速度（倍）", 1.0, 0.1, 100.0, 0.1
        )
        self.file_step_ms = _int_input(
            "固定回放步长（毫秒）", 1000, 10, 60_000, 10
        )
        self.load_market_button = pn.widgets.Button(
            label="加载或重新加载历史行情",
            icon="database-import",
            color="primary",
        )
        self.file_message = pn.pane.HTML("", sizing_mode="stretch_width")
        self.file_summary_table = pn.widgets.Tabulator(
            pd.DataFrame(),
            show_index=False,
            disabled=True,
            height=180,
            sizing_mode="stretch_width",
        )

        self.redis_enabled = pn.widgets.Toggle(
            label="启用Redis",
            value=False,
        )
        self.redis_host = pn.widgets.TextInput(label="Host", value="localhost")
        self.redis_port = _int_input("Port", 6379, 1, 65535, 1)
        self.redis_db = _int_input("DB", 0, 0, 100, 1)
        self.redis_password = pn.widgets.PasswordInput(label="Password")
        self.redis_key_prefix = pn.widgets.TextInput(
            label="Key prefix",
            value="etf_arbitrage",
        )
        self.redis_channel_pattern = pn.widgets.TextInput(
            label="Channel pattern",
            value="market:*",
        )
        self.redis_timeout = _float_input(
            "连接超时（秒）", 2.0, 0.1, 60.0, 0.1
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
            label="允许可选现金替代",
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
            "最大报价年龄（毫秒）", 5000, 1, 600_000, 100
        )
        self.max_skew = _int_input(
            "最大横截面时差（毫秒）", 5000, 1, 600_000, 100
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
        self.price_seed_mode.param.watch(self._price_seed_mode_changed, "value")
        self.load_market_button.on_click(self._load_market_data)
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
            self.price_seed_mode,
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
                    '<div class="exec-status"><b>跨境纸面实验：</b>'
                    '价格基准来自内网Redis、本地Redis采集记录或完全模拟；港股成分'
                    '多档深度、代理买卖、申赎确认和现金退补仍为模拟。引擎使用人民币折算后的'
                    '代理篮子估值，所有结果固定为INDICATIVE_PAPER，不连接交易柜台。</div>',
                    sizing_mode="stretch_width",
                    stylesheets=[EXECUTABLE_CSS],
                )
            )
        return pn.Column(
            *boundary,
            pn.Tabs(
                ("实时总览", runtime),
                ("数据与配置", self._configuration_view()),
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

    def _configuration_view(self):
        fx_controls = (
            pn.Card(
                pn.Column(
                    pn.Row(self.hkd_cny_mid, self.hkd_cny_spread_bps),
                    self.hkd_cny_volatility,
                    pn.pane.Markdown(
                        "模拟/本地历史回放使用这里的双边汇率；Redis最新价模式改用"
                        "指定Redis记录的买一/卖一。申购按汇率卖价，赎回按汇率买价。"
                    ),
                ),
                title="HKD/CNY快照参数",
                collapsed=False,
                collapsible=True,
                sizing_mode="stretch_width",
            )
            if self.cross_border
            else pn.Spacer(height=0)
        )
        self.fx_controls = fx_controls
        simulation_controls = pn.Column(
            pn.Row(self.scenario, self.random_seed),
            self.local_recording_path,
            pn.Row(self.redis_seed_suffix, self.redis_hkd_cny_code),
            self.price_seed_message,
            fx_controls,
            pn.Row(self.tick_ms, self.total_ticks, self.simulation_speed),
            pn.Row(self.premium_shock, self.base_volatility),
            pn.Row(self.book_levels, self.depth_per_level, self.quote_latency),
            pn.Row(self.stale_ratio, self.missing_ratio),
            pn.Card(
                pn.Column(
                    pn.Row(self.etf_spread, self.component_spread),
                    pn.Row(self.depth_decay, self.mean_reversion_speed),
                    pn.Row(self.shock_start_tick, self.shock_duration),
                    pn.Row(
                        self.suspended_weight,
                        self.limit_up_weight,
                        self.limit_down_weight,
                    ),
                    self.sequence_gap_probability,
                ),
                title="模拟行情高级参数",
                collapsed=False,
                collapsible=True,
                sizing_mode="stretch_width",
            ),
            sizing_mode="stretch_width",
        )
        self.simulation_controls = simulation_controls
        file_controls = pn.Column(
            self.market_upload,
            self.market_path,
            self.schema_mapping,
            pn.Row(self.file_speed, self.file_step_ms),
            self.load_market_button,
            self.file_message,
            self.file_summary_table,
            sizing_mode="stretch_width",
        )
        self.file_controls = file_controls
        redis_controls = pn.Column(
            self.redis_enabled,
            pn.Row(self.redis_host, self.redis_port, self.redis_db),
            self.redis_password,
            pn.Row(self.redis_key_prefix, self.redis_channel_pattern),
            self.redis_timeout,
            sizing_mode="stretch_width",
        )
        self.redis_controls = redis_controls
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
            pn.Row(self.max_age, self.max_skew, self.max_iopv_error),
            pn.Row(self.max_missing, self.max_stale, self.max_suspended),
            pn.Row(self.max_limit_up, self.max_limit_down, self.kill_switch),
            sizing_mode="stretch_width",
        )
        return pn.Column(
            pn.pane.Markdown("## 行情数据与执行配置"),
            self.pcf.view(),
            simulation_controls,
            file_controls,
            redis_controls,
            pn.Column(
                pn.Card(
                    execution_controls,
                    title="执行参数",
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
                    title="数据质量阈值",
                    collapsed=False,
                    collapsible=True,
                    sizing_mode="stretch_width",
                ),
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
        return PaperArbitrageConfig(
            etf_code=self.pcf.etf_code,
            data_source=self.source_mode.value,
            redis=RedisConfig(
                enabled=bool(self.redis_enabled.value),
                host=self.redis_host.value.strip() or "localhost",
                port=int(self.redis_port.value),
                db=int(self.redis_db.value),
                password=self.redis_password.value or None,
                key_prefix=self.redis_key_prefix.value.strip() or "etf_arbitrage",
                channel_pattern=self.redis_channel_pattern.value.strip() or "market:*",
                socket_timeout_seconds=float(self.redis_timeout.value),
            ),
            simulation=SimulationConfig(
                scenario=self.scenario.value,
                random_seed=int(self.random_seed.value),
                price_seed_mode=self.price_seed_mode.value,
                local_recording_path=self.local_recording_path.value.strip(),
                redis_code_suffix=self.redis_seed_suffix.value.strip() or ".SZ",
                redis_hkd_cny_code=(
                    self.redis_hkd_cny_code.value.strip()
                ),
                hkd_cny_mid=float(self.hkd_cny_mid.value),
                hkd_cny_spread_bps=float(self.hkd_cny_spread_bps.value),
                hkd_cny_volatility=float(self.hkd_cny_volatility.value),
                tick_interval_ms=int(self.tick_ms.value),
                total_ticks=int(self.total_ticks.value),
                simulation_speed=float(self.simulation_speed.value),
                base_volatility=float(self.base_volatility.value),
                etf_spread_bps=float(self.etf_spread.value),
                component_spread_bps=float(self.component_spread.value),
                number_of_book_levels=int(self.book_levels.value),
                depth_per_level=float(self.depth_per_level.value),
                depth_decay=float(self.depth_decay.value),
                premium_shock_bps=float(self.premium_shock.value),
                shock_start_tick=int(self.shock_start_tick.value),
                shock_duration_ticks=int(self.shock_duration.value),
                mean_reversion_speed=float(self.mean_reversion_speed.value),
                stale_quote_ratio=float(self.stale_ratio.value),
                missing_quote_ratio=float(self.missing_ratio.value),
                suspended_weight=float(self.suspended_weight.value),
                limit_up_weight=float(self.limit_up_weight.value),
                limit_down_weight=float(self.limit_down_weight.value),
                quote_latency_ms=int(self.quote_latency.value),
                sequence_gap_probability=float(
                    self.sequence_gap_probability.value
                ),
            ),
            file_replay=FileReplayConfig(
                path=self.market_path.value,
                fixed_step_ms=int(self.file_step_ms.value),
                playback_speed=float(self.file_speed.value),
                schema_mapping=self._schema_mapping(),
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
        signature = json.dumps(config.to_dict(), sort_keys=True, ensure_ascii=True)
        current_signature = (
            json.dumps(self.config.to_dict(), sort_keys=True, ensure_ascii=True)
            if self.config is not None
            else None
        )
        if not force and self.engine is not None and signature == current_signature:
            return

        previous_source = self.source
        self.stop_runtime()
        if isinstance(previous_source, RecordedHistoryMarketDataSource):
            previous_source.disconnect()
        self.config = config
        self.pcf_document = pcf
        self.pcf_report = report
        self.engine = PaperArbitrageEngine(runtime_pcf, config)
        self.redis_price_seed = None
        if config.data_source == DataSourceMode.SIMULATED:
            seed_mode = config.simulation.price_seed_mode
            local_path = _resolve_local_recording_path(
                self.project_root,
                pcf,
                config.simulation.local_recording_path,
            )
            use_local = seed_mode in {
                SimulationPriceSeedMode.AUTO_LOCAL,
                SimulationPriceSeedMode.LOCAL_RECORDING,
            }
            if use_local and local_path.exists():
                self.source = RecordedHistoryMarketDataSource(
                    local_path,
                    runtime_pcf,
                    config.simulation,
                )
                self.source.prime()
            elif seed_mode == SimulationPriceSeedMode.LOCAL_RECORDING:
                raise ValueError(
                    "未找到本地采集文件：{}".format(local_path)
                )
            elif (
                seed_mode == SimulationPriceSeedMode.AUTO_LOCAL
                and config.simulation.local_recording_path
            ):
                raise ValueError(
                    "指定的本地采集文件不存在：{}".format(local_path)
                )
            elif seed_mode == SimulationPriceSeedMode.REDIS_LATEST:
                self.redis_price_seed = _load_redis_price_seed(
                    pcf,
                    config.simulation.redis_code_suffix,
                    config.simulation.redis_hkd_cny_code,
                )
                self.source = SimulatedMarketDataSource(
                    runtime_pcf,
                    config.simulation,
                    initial_component_prices=(
                        self.redis_price_seed.component_prices
                    ),
                    initial_etf_price=self.redis_price_seed.etf_price,
                    initial_hkd_cny_bid=self.redis_price_seed.hkd_cny_bid,
                    initial_hkd_cny_ask=self.redis_price_seed.hkd_cny_ask,
                    price_seed_source="sz_redis_latest_trade",
                )
            else:
                self.source = SimulatedMarketDataSource(
                    runtime_pcf,
                    config.simulation,
                )
        elif config.data_source == DataSourceMode.FILE_REPLAY:
            if self.file_source is None:
                raise ValueError("请先上传或读取历史行情")
            self.file_source.reset()
            self.source = self.file_source
        else:
            self.source = RedisMarketDataSource(config.redis, config.etf_code)
        self.snapshot = None
        self.result = None
        self.history = _new_history()
        self._clear_sources()
        self._refresh_pcf_views()
        self._update_views()
        source_label = {
            DataSourceMode.SIMULATED: "模拟行情",
            DataSourceMode.FILE_REPLAY: "历史行情回放",
            DataSourceMode.REDIS: "标准化Redis",
        }.get(config.data_source, str(config.data_source))
        if isinstance(self.source, RecordedHistoryMarketDataSource):
            first_snapshot = self.source.prime()
            source_label = "本地历史数据（真实价格轨迹+合成盘口）"
            seed_text = (
                "逐条回放本地历史数据（{}），记录数={}，首条有效时间={}；"
                "ETF与成分股Last沿用历史轨迹，Bid/Ask与多档深度为模拟值。{}"
            ).format(
                self.source.path,
                self.source.total_records,
                first_snapshot.snapshot_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                (
                    " HKD/CNY为显式模拟双边快照：{:.6f}/{:.6f}。".format(
                        first_snapshot.hkd_cny_quote.bid,
                        first_snapshot.hkd_cny_quote.ask,
                    )
                    if first_snapshot.hkd_cny_quote
                    else ""
                ),
            )
        elif self.redis_price_seed is not None:
            seed_text = (
                "价格基准：Redis最新成交价，ETF={:.4f}，"
                "PCF实物成分股={}/{}；{}买卖盘深度与后续路径为模拟值。"
            ).format(
                self.redis_price_seed.etf_price,
                self.redis_price_seed.component_count,
                self.redis_price_seed.component_count,
                (
                    "HKD/CNY({})={:.6f}/{:.6f}；".format(
                        self.redis_price_seed.hkd_cny_code,
                        self.redis_price_seed.hkd_cny_bid,
                        self.redis_price_seed.hkd_cny_ask,
                    )
                    if self.redis_price_seed.hkd_cny_bid is not None
                    and self.redis_price_seed.hkd_cny_ask is not None
                    else ""
                ),
            )
        elif (
            config.data_source == DataSourceMode.SIMULATED
            and config.simulation.price_seed_mode
            == SimulationPriceSeedMode.AUTO_LOCAL
        ):
            seed_text = (
                "价格基准：未找到{}，已使用完全模拟。"
            ).format(local_path)
        else:
            seed_text = "价格基准：完全模拟。"
        self.price_seed_message.object = (
            '<div class="exec-status">{}</div>'.format(seed_text)
        )
        self._set_status(
            "配置已应用，数据源：{}。{}仅进行纸面模拟，不连接真实交易柜台。".format(
                source_label,
                seed_text,
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
            if self.callback is None:
                self.callback = pn.state.add_periodic_callback(
                    self._tick,
                    period=250,
                    start=True,
                )
            elif not self.callback.running:
                self.callback.start()
            self._set_status("连续模拟/回放中。", "running")
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
        except Exception as exc:
            self.source.stop()
            if self.callback is not None and self.callback.running:
                self.callback.stop()
            self._set_status("连续运行已停止：{}".format(exc), "error")

    def _steps_per_refresh(self) -> int:
        if self.source_mode.value == DataSourceMode.SIMULATED:
            tick_ms = int(self.tick_ms.value)
            speed = float(self.simulation_speed.value)
        else:
            tick_ms = int(self.file_step_ms.value)
            speed = float(self.file_speed.value)
        simulated_tick_seconds = tick_ms / 1000.0 / max(speed, 1e-9)
        return max(1, int(0.25 / simulated_tick_seconds + 0.999999))

    def _load_market_data(self, _event) -> None:
        try:
            mapping = self._schema_mapping()
            loader = DynamicMarketDataLoader()
            if self.market_upload.value:
                snapshots, summary = loader.load_bytes(
                    self.market_upload.value,
                    self.market_upload.filename or "uploaded.jsonl",
                    mapping,
                )
            else:
                snapshots, summary = loader.load_path(
                    self.market_path.value,
                    mapping,
                )
            self.file_source = FileReplayMarketDataSource(snapshots, summary)
            self.file_summary = summary
            self.file_summary_table.value = pd.DataFrame([asdict(summary)])
            self._set_file_message(
                "历史行情已加载：{}个快照，{}个证券。".format(
                    summary.snapshot_count,
                    summary.symbol_count,
                ),
                "success",
            )
            if self.source_mode.value == DataSourceMode.FILE_REPLAY:
                self._ensure_runtime(force=True)
        except Exception as exc:
            self._set_file_message(
                "历史行情加载失败：{}".format(exc),
                "error",
            )

    def _schema_mapping(self) -> Dict[str, str]:
        payload = json.loads(self.schema_mapping.value or "{}")
        if not isinstance(payload, dict):
            raise ValueError("Schema映射必须是JSON对象")
        return {str(key): str(value) for key, value in payload.items()}

    def _source_mode_changed(self, _event) -> None:
        mode = self.source_mode.value
        is_simulated = mode == DataSourceMode.SIMULATED
        if hasattr(self, "simulation_controls"):
            self.simulation_controls.visible = is_simulated
        if hasattr(self, "file_controls"):
            self.file_controls.visible = mode == DataSourceMode.FILE_REPLAY
        if hasattr(self, "redis_controls"):
            self.redis_controls.visible = mode == DataSourceMode.REDIS
        self.price_seed_mode.visible = is_simulated
        self.scenario.visible = is_simulated
        self.simulation_speed.visible = is_simulated
        self.total_ticks.visible = (
            is_simulated and not self._uses_local_history()
        )

    def _price_seed_mode_changed(self, _event) -> None:
        mode = self.price_seed_mode.value
        self.local_recording_path.visible = mode in {
            SimulationPriceSeedMode.AUTO_LOCAL,
            SimulationPriceSeedMode.LOCAL_RECORDING,
        }
        self.redis_seed_suffix.visible = (
            mode == SimulationPriceSeedMode.REDIS_LATEST
        )
        self.redis_hkd_cny_code.visible = (
            self.cross_border
            and mode == SimulationPriceSeedMode.REDIS_LATEST
        )
        message = {
            SimulationPriceSeedMode.AUTO_LOCAL: (
                "价格来源：本地历史数据。自动读取PCF交易日与ETF代码对应的"
                "tmp/recordings文件，逐条保留ETF与成分股Last历史轨迹，并为"
                "每个时点生成Bid/Ask和多档深度；不连接内网Redis。"
            ),
            SimulationPriceSeedMode.LOCAL_RECORDING: (
                "价格来源：指定的本地历史文件。逐条保留Last历史轨迹并生成"
                "模拟盘口；不连接内网Redis。"
            ),
            SimulationPriceSeedMode.SYNTHETIC: (
                "价格来源：完全模拟数据，不读取本地文件或内网Redis。"
            ),
            SimulationPriceSeedMode.REDIS_LATEST: (
                "价格来源：内网Redis实时最新价，需要当前进程配置SZ_REDIS_HOST。"
                "跨境模式允许汇率代码留空；此时ETF与成分股使用Redis最新价，"
                "纸面模拟汇率使用上方参数。"
            ),
        }[mode]
        self.price_seed_message.object = (
            '<div class="exec-status">{}</div>'.format(message)
        )
        self.total_ticks.visible = (
            self.source_mode.value == DataSourceMode.SIMULATED
            and not self._uses_local_history()
        )
        if _event is not None:
            self._set_status(
                "{} 请点击“应用配置并重置”或直接开始。".format(message),
                "ready",
            )

    def _uses_local_history(self) -> bool:
        return self.price_seed_mode.value in {
            SimulationPriceSeedMode.AUTO_LOCAL,
            SimulationPriceSeedMode.LOCAL_RECORDING,
        }

    def _pcf_changed(self) -> None:
        previous_source = self.source
        self.stop_runtime()
        if isinstance(previous_source, RecordedHistoryMarketDataSource):
            previous_source.disconnect()
        self.source = None
        self.engine = None
        self.config = None
        self.redis_price_seed = None
        self._refresh_pcf_views()
        self._set_status("PCF选择已变化，请应用配置。", "ready")

    def _build_price_figure(self):
        chart = figure(
            title="ETF价格、盘口与IOPV（实际价格窄幅缩放）",
            x_axis_type="datetime",
            y_range=DataRange1d(
                range_padding=0.08,
                range_padding_units="percent",
                only_visible=True,
                default_span=0.01,
            ),
            height=340,
            sizing_mode="stretch_width",
            tools="xpan,xwheel_zoom,box_zoom,reset,save",
            active_scroll="xwheel_zoom",
        )
        specs = (
            ("etf_last", "ETF最新价", ETF_COLOR, "solid", 2.2),
            ("etf_bid", "ETF买一价", BID_COLOR, "dotted", 1.4),
            ("etf_ask", "ETF卖一价", ASK_COLOR, "dotted", 1.4),
            ("internal_iopv", "内部IOPV", IOPV_COLOR, "solid", 1.8),
            ("lower_bound", "套利下界", "#8B5E34", "dotdash", 1.2),
            ("upper_bound", "套利上界", "#8B5E34", "dotdash", 1.2),
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
                    (
                        "套利边界",
                        "@lower_bound{0.0000} - @upper_bound{0.0000}",
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
            title="申购/赎回净利润边际",
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
        chart.line(
            "timestamp",
            "redemption_bps",
            source=self.market_source,
            legend_label="赎回净利润（bp）",
            color=NEGATIVE,
            line_width=2,
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
                "ETF价格、盘口与IOPV（开盘基准 {:.4f}=0 bp，右轴）"
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
            "creation_bps": [evaluation.creation.net_profit_bps],
            "redemption_bps": [evaluation.redemption.net_profit_bps],
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
                    "新交易许可",
                    "允许" if evaluation.quality.new_trades_enabled else "禁止",
                    BID_COLOR
                    if evaluation.quality.new_trades_enabled
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
                    "{:,.2f}".format(evaluation.creation.net_profit),
                    POSITIVE,
                ),
                (
                    "赎回净利润",
                    "{:,.2f}".format(evaluation.redemption.net_profit),
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
            DataSourceMode.SIMULATED: "模拟行情",
            DataSourceMode.FILE_REPLAY: "历史行情回放",
            DataSourceMode.REDIS: "标准化Redis",
        }.get(self.source_mode.value, str(self.source_mode.value))
        if isinstance(self.source, RecordedHistoryMarketDataSource):
            source_label = "本地历史数据（真实Last+模拟盘口）"
        self.runtime_status.object = (
            '<div class="exec-status">状态：就绪　数据源：{}　'
            "仅进行纸面模拟，不连接真实交易柜台。</div>"
        ).format(source_label)

    def _update_progress(self) -> None:
        if isinstance(self.source, RecordedHistoryMarketDataSource):
            total = max(self.source.total_records, 1)
            current = min(self.source.records_read, total)
            percentage = min(
                100,
                int(round(current / total * 100)),
            )
            self.progress.value = max(1, percentage) if current > 0 else 0
            self.progress.name = "回放进度 {}/{}".format(current, total)
        elif isinstance(self.source, SimulatedMarketDataSource):
            total = max(int(self.total_ticks.value), 1)
            self.progress.value = min(
                100,
                int(round(self.source.current_tick / total * 100)),
            )
            self.progress.name = "模拟进度 {}/{}".format(
                self.source.current_tick,
                total,
            )
        elif isinstance(self.source, FileReplayMarketDataSource):
            total = max(len(self.source.snapshots), 1)
            current = max(self.source._index + 1, 0)
            self.progress.value = min(100, int(round(current / total * 100)))
            self.progress.name = "文件回放进度 {}/{}".format(current, total)
        else:
            self.progress.value = 0
            self.progress.name = "进度"

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
                        "瓶颈证券": evaluation.creation.basket.bottleneck_symbol,
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
                        "瓶颈证券": evaluation.redemption.basket.bottleneck_symbol,
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
            "ETF价格、盘口与IOPV（实际价格窄幅缩放）"
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
    component_payload = {
        symbol: {
            "timestamp": book.exchange_timestamp.isoformat(),
            "status": book.trading_status.value,
            "bids": [[level.price, level.quantity] for level in book.bids],
            "asks": [[level.price, level.quantity] for level in book.asks],
        }
        for symbol, book in snapshot.component_order_books.items()
    }
    fx_quote = snapshot.hkd_cny_quote
    return {
        "timestamp": snapshot.snapshot_timestamp.isoformat(),
        "source_mode": snapshot.source_mode,
        "etf_code": snapshot.etf_order_book.symbol,
        "etf_last": snapshot.etf_order_book.last_price,
        "etf_bid": snapshot.etf_order_book.best_bid,
        "etf_ask": snapshot.etf_order_book.best_ask,
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
        "gross_profit": result.gross_profit,
        "estimated_costs": result.estimated_costs,
        "safety_buffer": result.safety_buffer,
        "net_profit": result.net_profit,
        "net_profit_bps": result.net_profit_bps,
        "executable": result.executable,
        "rejection_reasons": ",".join(result.rejection_reasons),
        "basket_value": result.basket.total_value,
        "basket_fully_filled": result.basket.fully_filled,
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


def _resolve_local_recording_path(
    project_root: Path,
    pcf,
    configured_path: str,
) -> Path:
    if configured_path:
        path = Path(configured_path).expanduser()
        return path if path.is_absolute() else project_root / path
    return (
        project_root
        / "tmp"
        / "recordings"
        / pcf.trading_day.strftime("%Y%m%d")
        / "{}.jsonl".format(pcf.etf_code)
    )


def _load_redis_price_seed(
    pcf,
    redis_code_suffix: str,
    hkd_cny_code: str,
):
    try:
        settings = SZRedisSettings.from_env()
    except ValueError as exc:
        raise ValueError(
            "启用Redis价格基准前，请先在启动Panel的同一终端设置SZ_REDIS_HOST"
        ) from exc
    client = SZRedisQuotationClient(settings)
    try:
        if not client.ping():
            raise ConnectionError("Redis连接探测未通过")
        return load_pcf_price_seed(
            client,
            pcf,
            trade_date=pcf.trading_day,
            redis_code_suffix=redis_code_suffix,
            hkd_cny_code=hkd_cny_code,
        )
    finally:
        connection = getattr(client, "_redis_client", None)
        close = getattr(connection, "close", None)
        if callable(close):
            close()


def _component_book_rows(snapshot) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "证券代码": symbol,
                "币种": "HKD" if str(book.exchange).upper() == "HKEX" else "CNY",
                "状态": book.trading_status.value,
                "最新价": book.last_price,
                "买一": book.best_bid,
                "买一量": book.bids[0].quantity if book.bids else None,
                "卖一": book.best_ask,
                "卖一量": book.asks[0].quantity if book.asks else None,
                "行情时间": book.exchange_timestamp,
                "序号": book.sequence_number,
            }
            for symbol, book in snapshot.component_order_books.items()
        ]
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
    return float(value) if _is_valid_price(value) else None
