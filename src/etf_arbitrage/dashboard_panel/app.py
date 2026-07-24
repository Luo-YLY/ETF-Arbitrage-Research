"""Panel control surface with incremental Bokeh market replay."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Union

import numpy as np
import pandas as pd
import panel as pn
from bokeh.models import ColumnDataSource, CrosshairTool, HoverTool, Span
from bokeh.plotting import figure

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
        if not self.datasets:
            raise RuntimeError("No observation datasets were found")
        self.by_day = datasets_by_day(self.datasets)
        self.frame = pd.DataFrame()
        self.cursor = 0
        self.callback: Optional[Any] = None
        self._changing_dataset = False

        days = list(self.by_day)
        self.day_select = pn.widgets.Select(
            label="交易日",
            options=days,
            value=days[0],
        )
        codes = self.by_day[self.day_select.value]
        self.etf_select = pn.widgets.Select(
            label="ETF",
            options=codes,
            value=codes[0],
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
        self.entry_threshold = pn.widgets.FloatInput(
            label="开仓阈值（%）",
            value=0.20,
            step=0.01,
            start=0.01,
            end=5.0,
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

        self.source = ColumnDataSource(data={key: [] for key in EMPTY_SOURCE})
        self.price_figure = self._build_price_figure()
        self.premium_figure = self._build_premium_figure()
        self.premium_figure.x_range = self.price_figure.x_range
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
        self.zero_span = Span(
            location=0,
            dimension="width",
            line_color="#596368",
            line_width=1,
        )
        self.premium_figure.add_layout(self.upper_span)
        self.premium_figure.add_layout(self.lower_span)
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
        self.progress = pn.indicators.Progress(
            label="回放进度",
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

        self.day_select.param.watch(self._dataset_changed, "value")
        self.etf_select.param.watch(self._dataset_changed, "value")
        self.entry_threshold.param.watch(self._threshold_changed, "value")
        self.play_button.on_click(self._play)
        self.pause_button.on_click(self._pause)
        self.step_button.on_click(self._single_step)
        self.reset_button.on_click(self._reset_clicked)
        self._load_selected_dataset()

    @property
    def dataset(self) -> ObservationDataset:
        return self.datasets[(self.day_select.value, self.etf_select.value)]

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

    def _dataset_changed(self, event: Any) -> None:
        if self._changing_dataset:
            return
        if event.obj is self.day_select:
            self._changing_dataset = True
            try:
                codes = self.by_day[self.day_select.value]
                self.etf_select.options = codes
                if self.etf_select.value not in codes:
                    self.etf_select.value = codes[0]
            finally:
                self._changing_dataset = False
        self._load_selected_dataset()

    def _load_selected_dataset(self) -> None:
        self._stop_callback()
        self.frame = load_observations(self.dataset)
        self.cursor = 0
        self._clear_source()
        self._update_quality_summary()
        self._stream_rows(min(2, len(self.frame)))
        self._update_status("ready")

    def _clear_source(self) -> None:
        self.source.data = {key: [] for key in EMPTY_SOURCE}

    def _play(self, _event: Any) -> None:
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
        self._stop_callback()
        self._stream_rows(1)
        if self.cursor < len(self.frame):
            self._update_status("paused")

    def _reset_clicked(self, _event: Any) -> None:
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
        payload = {
            "timestamp": list(rows["timestamp"]),
            "etf_price": list(rows["etf_price"]),
            "bid_price": list(rows["bid_price"]),
            "ask_price": list(rows["ask_price"]),
            "iopv": list(rows["iopv"]),
            "premium_pct": list(rows["premium"] * 100.0),
        }
        self.source.stream(payload, rollover=int(self.window_size.value))
        self.cursor = end
        self._update_metrics(rows.iloc[-1])
        self._update_table()
        self.progress.value = int(round(self.cursor / len(self.frame) * 100))
        self._update_status("running" if self.cursor < len(self.frame) else "completed")
        if self.cursor >= len(self.frame):
            self._stop_callback()

    def _threshold_changed(self, event: Any) -> None:
        threshold = float(event.new)
        self.upper_span.location = threshold
        self.lower_span.location = -threshold

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

    def _update_status(self, state: str) -> None:
        labels = {
            "ready": "就绪",
            "running": "回放中",
            "paused": "已暂停",
            "completed": "回放完成",
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
            "状态：{}　进度：{}/{}　行情时间：{}　数据源：{}"
            "</div>"
        ).format(
            labels.get(state, state),
            self.cursor,
            len(self.frame),
            time_text,
            self.dataset.source_kind,
        )

    def _update_table(self) -> None:
        columns = [
            column
            for column in (
                "timestamp",
                "etf_price",
                "iopv",
                "premium",
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
        quality = (
            self.frame["valuation_quality"].value_counts().to_dict()
            if "valuation_quality" in self.frame
            else {}
        )
        summary = pd.DataFrame(
            [
                {
                    "交易日": self.dataset.trade_date,
                    "ETF": self.dataset.etf_code,
                    "记录数": len(self.frame),
                    "开始时间": self.frame["timestamp"].min(),
                    "结束时间": self.frame["timestamp"].max(),
                    "最低折溢价": self.frame["premium"].min(),
                    "最高折溢价": self.frame["premium"].max(),
                    "最大缺失权重": self.frame["missing_weight"].max(),
                    "估值质量": ", ".join(
                        "{}={}".format(key, value) for key, value in quality.items()
                    ),
                    "来源": self.dataset.source_kind,
                    "文件": str(self.dataset.path.relative_to(self.project_root)),
                }
            ]
        )
        self.quality_table.value = summary
        self._update_status("ready")

    def template(self):
        controls = pn.Column(
            pn.pane.Markdown("### 数据与回放"),
            self.day_select,
            self.etf_select,
            self.speed_select,
            self.entry_threshold,
            self.window_size,
            pn.Row(self.play_button, self.pause_button),
            pn.Row(self.step_button, self.reset_button),
            self.progress,
            sizing_mode="stretch_width",
        )
        replay_view = pn.Column(
            self.metrics,
            self.status,
            pn.pane.Bokeh(self.price_figure, sizing_mode="stretch_width"),
            pn.pane.Bokeh(self.premium_figure, sizing_mode="stretch_width"),
            pn.pane.Markdown("#### 最近观察"),
            self.table,
            sizing_mode="stretch_width",
        )
        quality_view = pn.Column(
            pn.pane.Markdown("#### 数据完整性"),
            self.quality_table,
            sizing_mode="stretch_width",
        )
        return pn.template.FastListTemplate(
            title="深市ETF套利监控实验台",
            site="ETF Arbitrage",
            accent_base_color=ACCENT,
            header_background="#202A2E",
            sidebar=[controls],
            main=[
                pn.Tabs(
                    ("行情增量回放", replay_view),
                    ("数据状态", quality_view),
                    dynamic=False,
                    sizing_mode="stretch_width",
                )
            ],
            sidebar_width=300,
            main_layout=None,
            raw_css=[RAW_CSS],
        )


def build_panel_dashboard(project_root: Union[Path, str]):
    return PanelReplayDashboard(project_root).template()


def _metric_cell(label: str, value: str, accent: str) -> str:
    return (
        '<div class="metric-cell" style="--metric-accent:{accent}">'
        '<div class="metric-label">{label}</div>'
        '<div class="metric-value">{value}</div>'
        "</div>"
    ).format(label=label, value=value, accent=accent)
