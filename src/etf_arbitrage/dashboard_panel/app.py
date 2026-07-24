"""Panel control surface with incremental Bokeh market replay."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any, Optional, Union

import numpy as np
import pandas as pd
import panel as pn
from bokeh.models import ColumnDataSource, CrosshairTool, HoverTool, Span
from bokeh.plotting import figure

from etf_arbitrage.backtest import BacktestResult, PremiumBacktester
from etf_arbitrage.config import BacktestConfig
from etf_arbitrage.data import SZSE_ETFS

from .data import (
    ObservationDataset,
    datasets_by_day,
    discover_observation_datasets,
    load_observations,
)


pn.extension("tabulator", notifications=True, sizing_mode="stretch_width")

ACCENT = "#15616D"
POSITIVE = "#C44536"
NEGATIVE = "#3A6EA5"
ETF_COLOR = "#202A2E"
IOPV_COLOR = "#7A5C9E"
BID_COLOR = "#2F6B4F"
ASK_COLOR = "#C44536"

EMPTY_SOURCE = {
    "timestamp": [],
    "etf_price": [],
    "bid_price": [],
    "ask_price": [],
    "iopv": [],
    "premium_pct": [],
    "position": [],
    "action": [],
    "equity": [],
    "strategy_return": [],
    "open_equity": [],
    "close_equity": [],
}

RAW_CSS = """
:root {
  --panel-primary-color: #15616d;
}
.bk-header {
  box-shadow: none !important;
}
.metric-strip {
  display: grid;
  grid-template-columns: repeat(6, minmax(128px, 1fr));
  gap: 10px;
  width: 100%;
}
.metric-cell {
  min-height: 82px;
  padding: 12px 14px;
  border: 1px solid #d8dee1;
  border-top: 3px solid var(--metric-accent, #15616d);
  border-radius: 4px;
  background: #ffffff;
}
.metric-label {
  color: #657074;
  font-size: 12px;
  line-height: 1.2;
}
.metric-value {
  margin-top: 7px;
  color: #202a2e;
  font-size: 22px;
  font-weight: 650;
  line-height: 1.1;
}
.status-line {
  padding: 8px 10px;
  border-left: 3px solid #15616d;
  background: #f4f7f7;
  color: #374147;
  font-size: 13px;
}
@media (max-width: 1100px) {
  .metric-strip {
    grid-template-columns: repeat(3, minmax(128px, 1fr));
  }
}
"""


class PanelReplayDashboard:
    """Session-local replay controller backed by a persistent Bokeh source."""

    def __init__(self, project_root: Union[Path, str]) -> None:
        self.project_root = Path(project_root).resolve()
        self.datasets = discover_observation_datasets(self.project_root)
        self.by_day = datasets_by_day(self.datasets)
        self.observations = pd.DataFrame()
        self.frame = pd.DataFrame()
        self.backtest_result: Optional[BacktestResult] = None
        self.strategy_error = ""
        self.cursor = 0
        self.callback: Optional[Any] = None
        self._changing_dataset = False
        self._loaded_signature: Optional[tuple[str, int, int]] = None

        self.day_select = pn.widgets.DatePicker(
            label="研究日期",
            value=date.today(),
        )
        self.etf_select = pn.widgets.Select(
            label="ETF",
            options={
                "{} {}".format(code, profile.name): code
                for code, profile in SZSE_ETFS.items()
            },
            value="159915",
        )
        self.speed_select = pn.widgets.Select(
            label="回放速度",
            options={
                "1x": 1,
                "5x": 5,
                "20x": 20,
                "100x": 100,
            },
            value=5,
        )
        self.follow_latest = pn.widgets.Toggle(
            label="跟随最新行情",
            value=True,
            icon="live-view",
        )
        self.entry_threshold = pn.widgets.FloatInput(
            label="开仓阈值（%）",
            value=0.20,
            step=0.01,
            start=0.01,
            end=5.0,
        )
        self.exit_threshold = pn.widgets.FloatInput(
            label="平仓阈值（%）",
            value=0.05,
            step=0.01,
            start=0.0,
            end=2.0,
        )
        self.max_holding = pn.widgets.IntInput(
            label="最长持有期（快照数）",
            value=600,
            step=10,
            start=1,
            end=10000,
        )
        self.transaction_cost = pn.widgets.FloatInput(
            label="单边换仓成本（bp）",
            value=3.0,
            step=0.5,
            start=0.0,
            end=100.0,
        )
        self.execution_mode = pn.widgets.Select(
            label="回测价格模式",
            options={
                "指示性（最新价）": "indicative",
                "可执行（买一卖一）": "executable",
            },
            value="indicative",
        )
        self.window_size = pn.widgets.IntInput(
            label="图表保留点数",
            value=5000,
            step=500,
            start=500,
            end=20000,
        )
        self.play_button = pn.widgets.Button(
            label="开始",
            color="primary",
            icon="player-play",
        )
        self.pause_button = pn.widgets.Button(
            label="暂停",
            color="default",
            icon="player-pause",
        )
        self.step_button = pn.widgets.Button(
            label="单步",
            color="default",
            icon="player-skip-forward",
        )
        self.reset_button = pn.widgets.Button(
            label="重置",
            color="default",
            icon="refresh",
        )
        self.refresh_data_button = pn.widgets.Button(
            label="刷新行情数据",
            color="default",
            icon="database",
        )
        self.dataset_hint = pn.pane.HTML(
            "",
            sizing_mode="stretch_width",
        )

        self.source = ColumnDataSource(data={key: [] for key in EMPTY_SOURCE})
        self.price_figure = self._build_price_figure()
        self.premium_figure = self._build_premium_figure()
        self.strategy_figure = self._build_strategy_figure()
        self.premium_figure.x_range = self.price_figure.x_range
        self.strategy_figure.x_range = self.price_figure.x_range
        self.upper_span = Span(
            location=self.entry_threshold.value,
            dimension="width",
            line_color=POSITIVE,
            line_dash="dashed",
            line_width=1.2,
        )
        self.lower_span = Span(
            location=-self.entry_threshold.value,
            dimension="width",
            line_color=NEGATIVE,
            line_dash="dashed",
            line_width=1.2,
        )
        self.exit_upper_span = Span(
            location=self.exit_threshold.value,
            dimension="width",
            line_color="#7A5C9E",
            line_dash="dotdash",
            line_width=1,
        )
        self.exit_lower_span = Span(
            location=-self.exit_threshold.value,
            dimension="width",
            line_color="#7A5C9E",
            line_dash="dotdash",
            line_width=1,
        )
        self.zero_span = Span(
            location=0,
            dimension="width",
            line_color="#596368",
            line_width=1,
        )
        self.premium_figure.add_layout(self.upper_span)
        self.premium_figure.add_layout(self.lower_span)
        self.premium_figure.add_layout(self.exit_upper_span)
        self.premium_figure.add_layout(self.exit_lower_span)
        self.premium_figure.add_layout(self.zero_span)

        self.metrics = pn.pane.HTML(
            "",
            sizing_mode="stretch_width",
            stylesheets=[RAW_CSS],
        )
        self.status = pn.pane.HTML(
            "",
            sizing_mode="stretch_width",
            stylesheets=[RAW_CSS],
        )
        self.strategy_metrics = pn.pane.HTML(
            "",
            sizing_mode="stretch_width",
            stylesheets=[RAW_CSS],
        )
        self.progress = pn.indicators.Progress(
            label="数据进度",
            value=0,
            max=100,
            sizing_mode="stretch_width",
        )
        self.table = pn.widgets.Tabulator(
            pd.DataFrame(),
            show_index=False,
            disabled=True,
            pagination="remote",
            page_size=20,
            height=390,
            sizing_mode="stretch_width",
        )
        self.quality_table = pn.widgets.Tabulator(
            pd.DataFrame(),
            show_index=False,
            disabled=True,
            height=250,
            sizing_mode="stretch_width",
        )
        self.backtest_metrics = pn.pane.HTML(
            "",
            sizing_mode="stretch_width",
            stylesheets=[RAW_CSS],
        )
        self.trades_table = pn.widgets.Tabulator(
            pd.DataFrame(),
            show_index=False,
            disabled=True,
            pagination="remote",
            page_size=20,
            height=390,
            sizing_mode="stretch_width",
        )

        self.day_select.param.watch(self._dataset_changed, "value")
        self.etf_select.param.watch(self._dataset_changed, "value")
        self.follow_latest.param.watch(self._follow_latest_changed, "value")
        for widget in (
            self.entry_threshold,
            self.exit_threshold,
            self.max_holding,
            self.transaction_cost,
            self.execution_mode,
        ):
            widget.param.watch(self._strategy_parameters_changed, "value")
        self.play_button.on_click(self._play)
        self.pause_button.on_click(self._pause)
        self.step_button.on_click(self._single_step)
        self.reset_button.on_click(self._reset_clicked)
        self.refresh_data_button.on_click(self._refresh_data_clicked)
        self._load_selected_dataset()

    @property
    def dataset(self) -> Optional[ObservationDataset]:
        return self.datasets.get(
            (self.selected_day, str(self.etf_select.value))
        )

    @property
    def selected_day(self) -> str:
        value = self.day_select.value or date.today()
        return value.strftime("%Y%m%d")

    def _build_price_figure(self):
        chart = figure(
            title="ETF价格与IOPV",
            x_axis_type="datetime",
            height=340,
            sizing_mode="stretch_width",
            tools="xpan,xwheel_zoom,box_zoom,reset,save",
            active_scroll="xwheel_zoom",
        )
        chart.line(
            "timestamp",
            "etf_price",
            source=self.source,
            legend_label="ETF最新价",
            color=ETF_COLOR,
            line_width=2.2,
        )
        chart.line(
            "timestamp",
            "bid_price",
            source=self.source,
            legend_label="买一价",
            color=BID_COLOR,
            line_dash="dotted",
            line_width=1.3,
        )
        chart.line(
            "timestamp",
            "ask_price",
            source=self.source,
            legend_label="卖一价",
            color=ASK_COLOR,
            line_dash="dotted",
            line_width=1.3,
        )
        chart.line(
            "timestamp",
            "iopv",
            source=self.source,
            legend_label="IOPV",
            color=IOPV_COLOR,
            line_width=1.8,
        )
        chart.add_tools(
            CrosshairTool(dimensions="height"),
            HoverTool(
                tooltips=[
                    ("时间", "@timestamp{%F %T}"),
                    ("ETF", "@etf_price{0.0000}"),
                    ("买一", "@bid_price{0.0000}"),
                    ("卖一", "@ask_price{0.0000}"),
                    ("IOPV", "@iopv{0.0000}"),
                ],
                formatters={"@timestamp": "datetime"},
                mode="vline",
            ),
        )
        chart.legend.orientation = "horizontal"
        chart.legend.location = "top_left"
        chart.legend.click_policy = "hide"
        chart.grid.grid_line_color = "#e4e8ea"
        chart.outline_line_color = "#cfd5d8"
        return chart

    def _build_premium_figure(self):
        chart = figure(
            title="折溢价率",
            x_axis_type="datetime",
            height=300,
            sizing_mode="stretch_width",
            tools="xpan,xwheel_zoom,box_zoom,reset,save",
            active_scroll="xwheel_zoom",
        )
        chart.line(
            "timestamp",
            "premium_pct",
            source=self.source,
            legend_label="Premium",
            color=ACCENT,
            line_width=2,
        )
        chart.add_tools(
            CrosshairTool(dimensions="height"),
            HoverTool(
                tooltips=[
                    ("时间", "@timestamp{%F %T}"),
                    ("折溢价", "@premium_pct{+0.000}%"),
                ],
                formatters={"@timestamp": "datetime"},
                mode="vline",
            ),
        )
        chart.yaxis.axis_label = "折溢价率（%）"
        chart.grid.grid_line_color = "#e4e8ea"
        chart.outline_line_color = "#cfd5d8"
        return chart

    def _build_strategy_figure(self):
        chart = figure(
            title="均值回复累计价差收益指数",
            x_axis_type="datetime",
            height=300,
            sizing_mode="stretch_width",
            tools="xpan,xwheel_zoom,box_zoom,reset,save",
            active_scroll="xwheel_zoom",
        )
        chart.line(
            "timestamp",
            "equity",
            source=self.source,
            legend_label="累计价差收益指数",
            color=ACCENT,
            line_width=2,
        )
        chart.scatter(
            "timestamp",
            "open_equity",
            source=self.source,
            legend_label="开仓",
            color=POSITIVE,
            marker="triangle",
            size=9,
        )
        chart.scatter(
            "timestamp",
            "close_equity",
            source=self.source,
            legend_label="平仓",
            color=NEGATIVE,
            marker="inverted_triangle",
            size=9,
        )
        chart.add_tools(
            CrosshairTool(dimensions="height"),
            HoverTool(
                tooltips=[
                    ("时间", "@timestamp{%F %T}"),
                    ("收益指数", "@equity{0.000000}"),
                    ("仓位", "@position"),
                    ("动作", "@action"),
                ],
                formatters={"@timestamp": "datetime"},
                mode="vline",
            ),
        )
        chart.yaxis.axis_label = "累计价差收益指数"
        chart.legend.orientation = "horizontal"
        chart.legend.location = "top_left"
        chart.legend.click_policy = "hide"
        chart.grid.grid_line_color = "#e4e8ea"
        chart.outline_line_color = "#cfd5d8"
        return chart

    def _dataset_changed(self, event: Any) -> None:
        if self._changing_dataset:
            return
        self._load_selected_dataset()

    def refresh_datasets(self) -> None:
        datasets = discover_observation_datasets(self.project_root)
        if not datasets:
            self._load_selected_dataset()
            return
        self.datasets = datasets
        self.by_day = datasets_by_day(datasets)
        self._load_selected_dataset(skip_unchanged=True)

    def _refresh_data_clicked(self, _event: Any) -> None:
        self.refresh_datasets()

    def _load_selected_dataset(self, skip_unchanged: bool = False) -> None:
        self._stop_callback()
        dataset = self.dataset
        if dataset is None:
            self._loaded_signature = None
            self.observations = pd.DataFrame(
                columns=[
                    "timestamp",
                    "ETF_code",
                    "etf_price",
                    "bid_price",
                    "ask_price",
                    "iopv",
                    "premium",
                    "missing_weight",
                    "valuation_quality",
                ]
            )
            self.frame = self.observations.assign(
                position=pd.Series(dtype=int),
                action=pd.Series(dtype=str),
                strategy_return=pd.Series(dtype=float),
                equity=pd.Series(dtype=float),
            )
            self.backtest_result = None
            self.strategy_error = "尚未发现真实行情记录"
            self._set_dataset_hint(
                "{} / {} 尚无行情记录，等待采集或导入。".format(
                    self.selected_day,
                    self.etf_select.value,
                ),
                "waiting",
            )
            self.cursor = 0
            self._clear_source()
            self._update_quality_summary()
            self._update_table()
            self._update_status("ready")
            return
        signature = self._dataset_signature(dataset)
        if (
            skip_unchanged
            and self.follow_latest.value
            and signature == self._loaded_signature
        ):
            self._update_status("following")
            return
        self.observations = load_observations(dataset)
        self._loaded_signature = signature
        self._set_dataset_hint(
            "{} / {} 已加载 {:,} 条记录。".format(
                dataset.trade_date,
                dataset.etf_code,
                len(self.observations),
            ),
            "ready",
        )
        self._recompute_backtest()
        self._update_quality_summary()
        if self.follow_latest.value:
            self._show_latest_window()
        else:
            self.cursor = 0
            self._clear_source()
            self._stream_rows(min(2, len(self.frame)))
            self._update_status("ready")

    def _clear_source(self) -> None:
        self.source.data = {key: [] for key in EMPTY_SOURCE}

    def _play(self, _event: Any) -> None:
        self.follow_latest.value = False
        if self.cursor >= len(self.frame):
            self.cursor = 0
            self._clear_source()
        if self.callback is None:
            self.callback = pn.state.add_periodic_callback(
                self._tick,
                period=250,
                start=True,
            )
        elif not self.callback.running:
            self.callback.start()
        self._update_status("running")

    def _pause(self, _event: Any) -> None:
        self._stop_callback()
        self._update_status("paused")

    def _single_step(self, _event: Any) -> None:
        self.follow_latest.value = False
        self._stop_callback()
        self._stream_rows(1)
        if self.cursor < len(self.frame):
            self._update_status("paused")

    def _reset_clicked(self, _event: Any) -> None:
        self.follow_latest.value = False
        self._stop_callback()
        self.cursor = 0
        self._clear_source()
        self._stream_rows(min(2, len(self.frame)))
        self._update_status("ready")

    def _tick(self) -> None:
        self._stream_rows(int(self.speed_select.value))

    def _stream_rows(self, count: int) -> None:
        if self.frame.empty or self.cursor >= len(self.frame):
            self._stop_callback()
            self._update_status("completed")
            return
        end = min(len(self.frame), self.cursor + max(1, count))
        rows = self.frame.iloc[self.cursor:end]
        payload = self._rows_payload(rows)
        self.source.stream(payload, rollover=int(self.window_size.value))
        self.cursor = end
        self._update_metrics(rows.iloc[-1])
        self._update_strategy_metrics(rows.iloc[-1])
        self._update_table()
        self.progress.value = int(round(self.cursor / len(self.frame) * 100))
        self._update_status("running" if self.cursor < len(self.frame) else "completed")
        if self.cursor >= len(self.frame):
            self._stop_callback()

    @staticmethod
    def _rows_payload(rows: pd.DataFrame) -> dict[str, list[Any]]:
        return {
            "timestamp": list(rows["timestamp"]),
            "etf_price": list(rows["etf_price"]),
            "bid_price": list(rows["bid_price"]),
            "ask_price": list(rows["ask_price"]),
            "iopv": list(rows["iopv"]),
            "premium_pct": list(rows["premium"] * 100.0),
            "position": list(rows["position"]),
            "action": list(rows["action"]),
            "equity": list(rows["equity"]),
            "strategy_return": list(rows["strategy_return"]),
            "open_equity": list(
                rows["equity"].where(rows["action"].str.startswith("open_"))
            ),
            "close_equity": list(
                rows["equity"].where(rows["action"].str.startswith("close_"))
            ),
        }

    def _show_latest_window(self) -> None:
        if self.frame.empty:
            self._clear_source()
            self.cursor = 0
            self.progress.value = 0
            self._update_status("ready")
            return
        count = min(len(self.frame), int(self.window_size.value))
        rows = self.frame.iloc[-count:]
        self.source.data = self._rows_payload(rows)
        self.cursor = len(self.frame)
        self._update_metrics(rows.iloc[-1])
        self._update_strategy_metrics(rows.iloc[-1])
        self._update_table()
        self.progress.value = 100
        self._update_status("following")

    def _follow_latest_changed(self, event: Any) -> None:
        if event.new:
            self._stop_callback()
            self.refresh_datasets()

    @staticmethod
    def _dataset_signature(
        dataset: ObservationDataset,
    ) -> tuple[str, int, int]:
        stat = dataset.path.stat()
        return str(dataset.path), stat.st_mtime_ns, stat.st_size

    def _strategy_parameters_changed(self, _event: Any) -> None:
        self._stop_callback()
        if self.follow_latest.value:
            self.upper_span.location = float(self.entry_threshold.value)
            self.lower_span.location = -float(self.entry_threshold.value)
            self.exit_upper_span.location = float(self.exit_threshold.value)
            self.exit_lower_span.location = -float(self.exit_threshold.value)
            self._recompute_backtest()
            self._update_quality_summary()
            self._show_latest_window()
            return
        target_cursor = min(max(self.cursor, 2), len(self.observations))
        self.upper_span.location = float(self.entry_threshold.value)
        self.lower_span.location = -float(self.entry_threshold.value)
        self.exit_upper_span.location = float(self.exit_threshold.value)
        self.exit_lower_span.location = -float(self.exit_threshold.value)
        self._recompute_backtest()
        self.cursor = 0
        self._clear_source()
        self._update_quality_summary()
        self._stream_rows(target_cursor)
        if self.cursor < len(self.frame):
            self._update_status("paused")

    def _recompute_backtest(self) -> None:
        backtest_input = self.observations.copy()
        mode = str(self.execution_mode.value)
        if mode == "indicative" and "indicative_risk_blocked" in backtest_input:
            backtest_input["risk_blocked"] = (
                backtest_input["indicative_risk_blocked"].fillna(False).astype(bool)
            )
        for column in ("premium_at_bid", "discount_at_ask"):
            if column not in backtest_input:
                backtest_input[column] = float("nan")

        try:
            result = PremiumBacktester(
                BacktestConfig(
                    entry_threshold=float(self.entry_threshold.value) / 100.0,
                    exit_threshold=float(self.exit_threshold.value) / 100.0,
                    max_holding_periods=int(self.max_holding.value),
                    transaction_cost_bps=float(self.transaction_cost.value),
                    execution_mode=mode,
                )
            ).run(backtest_input)
            self.backtest_result = result
            self.strategy_error = ""
            self.frame = result.timeline
        except ValueError as exc:
            self.backtest_result = None
            self.strategy_error = str(exc)
            self.frame = backtest_input.copy()
            self.frame["position"] = 0
            self.frame["action"] = "flat"
            self.frame["strategy_return"] = 0.0
            self.frame["equity"] = 1.0

    def _stop_callback(self) -> None:
        if self.callback is not None and self.callback.running:
            self.callback.stop()

    def _update_metrics(self, row: pd.Series) -> None:
        bid = row.get("bid_price")
        ask = row.get("ask_price")
        spread = (
            (float(ask) - float(bid)) / ((float(ask) + float(bid)) / 2) * 10000
            if pd.notna(bid) and pd.notna(ask) and float(bid) > 0 and float(ask) > 0
            else float("nan")
        )
        premium = float(row["premium"])
        self.metrics.object = """
        <div class="metric-strip">
          {cells}
        </div>
        """.format(
            cells="".join(
                [
                    _metric_cell("ETF最新价", "{:.4f}".format(row["etf_price"]), ETF_COLOR),
                    _metric_cell("IOPV", "{:.4f}".format(row["iopv"]), IOPV_COLOR),
                    _metric_cell(
                        "折溢价率",
                        "{:+.3%}".format(premium),
                        POSITIVE if premium >= 0 else NEGATIVE,
                    ),
                    _metric_cell(
                        "买一 / 卖一",
                        "{:.4f} / {:.4f}".format(bid, ask)
                        if pd.notna(bid) and pd.notna(ask)
                        else "暂缺",
                        BID_COLOR,
                    ),
                    _metric_cell(
                        "价差",
                        "{:.2f} bp".format(spread) if np.isfinite(spread) else "暂缺",
                        ASK_COLOR,
                    ),
                    _metric_cell(
                        "估值质量",
                        str(row.get("valuation_quality", "未知")),
                        ACCENT,
                    ),
                ]
            )
        )

    def _update_strategy_metrics(self, row: pd.Series) -> None:
        prefix = self.frame.iloc[:self.cursor]
        equity = float(row.get("equity", 1.0))
        drawdown = prefix["equity"] / prefix["equity"].cummax() - 1.0
        timestamp = pd.Timestamp(row["timestamp"]).to_pydatetime()
        completed_trades = (
            [
                trade
                for trade in self.backtest_result.trades
                if trade.exit_time <= timestamp
            ]
            if self.backtest_result is not None
            else []
        )
        win_rate = (
            sum(trade.net_return > 0 for trade in completed_trades)
            / len(completed_trades)
            if completed_trades
            else 0.0
        )
        position = int(row.get("position", 0))
        action = str(row.get("action", "flat"))
        self.strategy_metrics.object = """
        <div class="metric-strip">
          {cells}
        </div>
        """.format(
            cells="".join(
                [
                    _metric_cell(
                        "当前仓位",
                        _position_label(position),
                        POSITIVE if position < 0 else NEGATIVE if position > 0 else ACCENT,
                    ),
                    _metric_cell("当前动作", _action_label(action), IOPV_COLOR),
                    _metric_cell(
                        "累计价差收益",
                        "{:+.3%}".format(equity - 1.0),
                        POSITIVE if equity >= 1.0 else NEGATIVE,
                    ),
                    _metric_cell("已平仓", str(len(completed_trades)), ETF_COLOR),
                    _metric_cell("胜率", "{:.1%}".format(win_rate), BID_COLOR),
                    _metric_cell(
                        "最大回撤",
                        "{:.3%}".format(float(drawdown.min())),
                        ASK_COLOR,
                    ),
                ]
            )
        )

    def _update_status(self, state: str) -> None:
        labels = {
            "ready": "就绪",
            "running": "回放中",
            "paused": "已暂停",
            "completed": "回放完成",
            "following": "跟随最新",
        }
        timestamp = (
            self.frame.iloc[min(max(self.cursor - 1, 0), len(self.frame) - 1)]["timestamp"]
            if not self.frame.empty
            else None
        )
        time_text = (
            pd.Timestamp(timestamp).strftime("%H:%M:%S")
            if pd.notna(timestamp)
            else "--"
        )
        self.status.object = (
            '<div class="status-line">'
            "状态：{}　进度：{}/{}　行情时间：{}　模型：{}　数据源：{}{}"
            "</div>"
        ).format(
            labels.get(state, state),
            self.cursor,
            len(self.frame),
            time_text,
            (
                "指示性（最新价）"
                if self.execution_mode.value == "indicative"
                else "可执行（买一卖一）"
            ),
            self.dataset.source_kind if self.dataset is not None else "等待采集",
            "　参数错误：{}".format(self.strategy_error) if self.strategy_error else "",
        )

    def _update_table(self) -> None:
        columns = [
            column
            for column in (
                "timestamp",
                "etf_price",
                "iopv",
                "premium",
                "position",
                "action",
                "equity",
                "valuation_quality",
                "risk_level",
                "risk_blockers",
            )
            if column in self.frame
        ]
        values = self.frame.iloc[max(0, self.cursor - 200):self.cursor][columns].copy()
        if "premium" in values:
            values["premium"] = values["premium"] * 100.0
            values.rename(columns={"premium": "premium_pct"}, inplace=True)
        self.table.value = values.iloc[::-1].reset_index(drop=True)

    def _update_quality_summary(self) -> None:
        dataset = self.dataset
        if dataset is None or self.frame.empty:
            self.quality_table.value = pd.DataFrame(
                [
                    {
                        "状态": "尚未发现行情记录",
                        "交易日": self.day_select.value,
                        "ETF": self.etf_select.value,
                    }
                ]
            )
            self.backtest_metrics.object = ""
            self.trades_table.value = pd.DataFrame()
            return
        quality = (
            self.frame["valuation_quality"].value_counts().to_dict()
            if "valuation_quality" in self.frame
            else {}
        )
        summary = pd.DataFrame(
            [
                {
                    "交易日": dataset.trade_date,
                    "ETF": dataset.etf_code,
                    "记录数": len(self.frame),
                    "开始时间": self.frame["timestamp"].min(),
                    "结束时间": self.frame["timestamp"].max(),
                    "最低折溢价": self.frame["premium"].min(),
                    "最高折溢价": self.frame["premium"].max(),
                    "最大缺失权重": self.frame["missing_weight"].max(),
                    "估值质量": ", ".join(
                        "{}={}".format(key, value) for key, value in quality.items()
                    ),
                    "模型交易数": (
                        len(self.backtest_result.trades)
                        if self.backtest_result is not None
                        else 0
                    ),
                    "模型收益": (
                        self.backtest_result.performance.total_return
                        if self.backtest_result is not None
                        else 0.0
                    ),
                    "模型最大回撤": (
                        self.backtest_result.performance.max_drawdown
                        if self.backtest_result is not None
                        else 0.0
                    ),
                    "来源": dataset.source_kind,
                    "文件": str(dataset.path.relative_to(self.project_root)),
                }
            ]
        )
        self.quality_table.value = summary
        if self.backtest_result is None:
            self.backtest_metrics.object = ""
            self.trades_table.value = pd.DataFrame()
        else:
            result = self.backtest_result
            self.backtest_metrics.object = """
            <div class="metric-strip">
              {cells}
            </div>
            """.format(
                cells="".join(
                    [
                        _metric_cell(
                            "价差收益",
                            "{:+.3%}".format(result.performance.total_return),
                            ACCENT,
                        ),
                        _metric_cell(
                            "Sharpe",
                            "{:.2f}".format(result.performance.sharpe),
                            IOPV_COLOR,
                        ),
                        _metric_cell(
                            "最大回撤",
                            "{:.3%}".format(result.performance.max_drawdown),
                            ASK_COLOR,
                        ),
                        _metric_cell(
                            "胜率",
                            "{:.1%}".format(result.win_rate),
                            BID_COLOR,
                        ),
                        _metric_cell(
                            "平均持有",
                            "{:.1f}个快照".format(
                                result.average_holding_periods
                            ),
                            ETF_COLOR,
                        ),
                        _metric_cell(
                            "已平仓",
                            str(len(result.trades)),
                            POSITIVE,
                        ),
                    ]
                )
            )
            self.trades_table.value = pd.DataFrame(
                [asdict(trade) for trade in result.trades]
            )
        self._update_status("ready")

    def sidebar_controls(self):
        model_controls = pn.Column(
            self.entry_threshold,
            self.exit_threshold,
            self.max_holding,
            self.transaction_cost,
            self.execution_mode,
            sizing_mode="stretch_width",
        )
        replay_controls = pn.Column(
            self.follow_latest,
            self.speed_select,
            self.window_size,
            pn.Row(self.play_button, self.pause_button),
            pn.Row(self.step_button, self.reset_button),
            self.progress,
            sizing_mode="stretch_width",
        )
        return pn.Column(
            pn.pane.Markdown("### 实盘均值回复监控"),
            self.day_select,
            self.etf_select,
            self.dataset_hint,
            self.refresh_data_button,
            pn.Accordion(
                ("均值回复参数", model_controls),
                ("回放设置", replay_controls),
                active=[],
                sizing_mode="stretch_width",
            ),
            sizing_mode="stretch_width",
        )

    def _set_dataset_hint(self, text: str, state: str) -> None:
        colors = {
            "ready": ("#2F6B4F", "#eef7f2"),
            "waiting": ("#8B5E34", "#fff8ec"),
        }
        foreground, background = colors.get(
            state, ("#374147", "#f4f7f7")
        )
        self.dataset_hint.object = (
            '<div style="border-left:3px solid {fg};background:{bg};'
            'padding:8px 10px;color:{fg};font-size:12px">{text}</div>'
        ).format(fg=foreground, bg=background, text=text)

    def replay_view(self):
        return pn.Column(
            self.metrics,
            self.status,
            pn.pane.Bokeh(self.price_figure, sizing_mode="stretch_width"),
            pn.pane.Bokeh(self.premium_figure, sizing_mode="stretch_width"),
            self.strategy_metrics,
            pn.pane.Bokeh(self.strategy_figure, sizing_mode="stretch_width"),
            pn.pane.Markdown("#### 最近观察"),
            self.table,
            sizing_mode="stretch_width",
        )

    def backtest_view(self):
        return pn.Column(
            pn.pane.Markdown("## 收盘后均值回复回测"),
            self.backtest_metrics,
            pn.pane.Markdown("#### 完整交易记录"),
            self.trades_table,
            sizing_mode="stretch_width",
        )

    def quality_view(self):
        return pn.Column(
            pn.pane.Markdown("#### 数据完整性"),
            self.quality_table,
            sizing_mode="stretch_width",
        )

    def stop_runtime(self) -> None:
        self._stop_callback()

    def template(self):
        return pn.template.FastListTemplate(
            title="深市ETF均值回复监控实验台",
            site="ETF Arbitrage",
            accent_base_color=ACCENT,
            header_background="#202A2E",
            sidebar=[self.sidebar_controls()],
            main=[
                pn.Tabs(
                    ("均值回复监控", self.replay_view()),
                    ("收盘回测", self.backtest_view()),
                    ("数据状态", self.quality_view()),
                    dynamic=True,
                    sizing_mode="stretch_width",
                )
            ],
            sidebar_width=300,
            main_layout=None,
            raw_css=[RAW_CSS],
        )


def build_panel_dashboard(project_root: Union[Path, str]):
    from .console import PanelConsoleDashboard

    return PanelConsoleDashboard(project_root).template()


def _metric_cell(label: str, value: str, accent: str) -> str:
    return (
        '<div class="metric-cell" style="--metric-accent:{accent}">'
        '<div class="metric-label">{label}</div>'
        '<div class="metric-value">{value}</div>'
        "</div>"
    ).format(label=label, value=value, accent=accent)


def _position_label(position: int) -> str:
    return {
        -1: "做空溢价",
        0: "空仓",
        1: "做多折价",
    }.get(position, "未知")


def _action_label(action: str) -> str:
    labels = {
        "flat": "观望",
        "hold": "持有",
        "open_premium": "开仓：做空溢价",
        "open_discount": "开仓：做多折价",
        "close_converged": "平仓：偏离收敛",
        "close_max_holding": "平仓：持有超时",
        "close_risk_blocked": "平仓：风险阻断",
        "close_invalid_data": "平仓：数据失效",
        "close_end_of_replay": "平仓：回放结束",
    }
    return labels.get(action, action)
