"""Panel controls for the real Redis collection and PCF preparation workflow."""

from __future__ import annotations

from datetime import date, datetime
import os
from pathlib import Path
from typing import Callable, Optional, Union

import pandas as pd
import panel as pn

from etf_arbitrage.data import SZSE_ETFS
from etf_arbitrage.operations import (
    MarketMonitorController,
    MarketMonitorJob,
    SZSEMarketSchedule,
    read_job_state,
)

from .pcf_controls import PanelPCFControls


PHASE_LABELS = {
    "closed": "休市",
    "before_open": "盘前",
    "morning_session": "早盘",
    "lunch_break": "午间休市",
    "afternoon_session": "午盘",
    "after_close": "已收盘",
}


class MeanReversionOperations:
    """Operational surface for the real-quote mean-reversion page."""

    def __init__(
        self,
        project_root: Union[Path, str],
        on_observations_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.controller = MarketMonitorController(self.project_root)
        self.schedule = SZSEMarketSchedule()
        self.on_observations_changed = on_observations_changed
        self.callback = None

        self.pcf = PanelPCFControls(
            self.project_root,
            default_date=date.today(),
        )
        self.monitor_etfs = pn.widgets.MultiChoice(
            label="监控ETF",
            options=list(SZSE_ETFS),
            value=["159915"],
        )
        self.interval = pn.widgets.FloatInput(
            label="采集间隔（秒）",
            value=3.0,
            start=1.0,
            end=30.0,
            step=1.0,
        )
        self.redis_suffix = pn.widgets.TextInput(
            label="Redis代码后缀",
            value=".SZ",
        )
        self.start_button = pn.widgets.Button(
            label="启动当日采集",
            icon="player-play",
            color="primary",
        )
        self.stop_button = pn.widgets.Button(
            label="停止采集",
            icon="player-stop",
            color="warning",
        )
        self.refresh_button = pn.widgets.Button(
            label="刷新状态与数据",
            icon="refresh",
        )
        self.auto_refresh = pn.widgets.Toggle(
            label="自动刷新任务状态",
            value=False,
            icon="refresh",
        )
        self.message = pn.pane.HTML("", sizing_mode="stretch_width")
        self.status_table = pn.widgets.Tabulator(
            pd.DataFrame(),
            show_index=False,
            disabled=True,
            height=155,
            sizing_mode="stretch_width",
        )
        self.pcf_table = pn.widgets.Tabulator(
            pd.DataFrame(),
            show_index=False,
            disabled=True,
            height=210,
            sizing_mode="stretch_width",
        )
        self.latest_table = pn.widgets.Tabulator(
            pd.DataFrame(),
            show_index=False,
            disabled=True,
            height=250,
            sizing_mode="stretch_width",
        )
        self.log_pane = pn.widgets.TextAreaInput(
            label="",
            value="",
            disabled=True,
            height=260,
            sizing_mode="stretch_width",
            styles={
                "background": "#151b1e",
                "color": "#dce5e8",
                "padding": "10px",
                "overflow-y": "auto",
            },
        )

        self.monitor_etfs.param.watch(self._monitor_selection_changed, "value")
        self.pcf.on_change(self.refresh)
        self.start_button.on_click(self._start)
        self.stop_button.on_click(self._stop)
        self.refresh_button.on_click(self._refresh_clicked)
        self.auto_refresh.param.watch(self._auto_refresh_changed, "value")
        self.refresh()

    @property
    def trading_day(self) -> str:
        return self.pcf.trading_day

    def view(self):
        return pn.Column(
            pn.pane.Markdown("## 盘前与真实行情采集"),
            pn.Tabs(
                (
                    "PCF准备",
                    pn.Column(
                        self.pcf.view(),
                        pn.pane.Markdown("#### 本次监控标的PCF状态"),
                        self.pcf_table,
                    ),
                ),
                (
                    "日内采集",
                    pn.Column(
                        pn.Row(self.monitor_etfs, self.interval),
                        self.redis_suffix,
                        pn.Row(
                            self.start_button,
                            self.stop_button,
                            self.refresh_button,
                            self.auto_refresh,
                        ),
                        self.message,
                        self.status_table,
                        pn.pane.Markdown("#### 最新采集状态"),
                        self.latest_table,
                        pn.pane.Markdown("#### 采集日志"),
                        self.log_pane,
                    ),
                ),
                dynamic=True,
                sizing_mode="stretch_width",
            ),
            sizing_mode="stretch_width",
        )

    def refresh(self) -> None:
        selected = list(self.monitor_etfs.value)
        rows = []
        for code in selected:
            try:
                path = self.pcf.repository.find(code, self.trading_day)
                rows.append(
                    {
                        "ETF": code,
                        "名称": SZSE_ETFS[code].name,
                        "状态": "已校验" if path is not None else "缺失",
                        "本地文件": str(path.relative_to(self.project_root))
                        if path is not None
                        else "",
                    }
                )
            except Exception as exc:
                rows.append(
                    {
                        "ETF": code,
                        "名称": SZSE_ETFS[code].name,
                        "状态": "校验失败",
                        "错误": str(exc),
                    }
                )
        self.pcf_table.value = pd.DataFrame(rows)

        state = read_job_state(self.controller.state_path(self.trading_day))
        phase = self.schedule.phase(datetime.now())
        status = state.get("status", "未启动")
        self.status_table.value = pd.DataFrame(
            [
                {
                    "交易日": self.trading_day,
                    "当前时段": PHASE_LABELS.get(phase, phase),
                    "Redis配置": (
                        "已配置" if os.getenv("SZ_REDIS_HOST") else "未配置"
                    ),
                    "任务状态": status,
                    "进程号": state.get("pid", ""),
                    "累计轮询": state.get("polls", 0),
                    "监控ETF": ",".join(state.get("etf_codes", selected)),
                }
            ]
        )
        latest = state.get("latest", {})
        self.latest_table.value = pd.DataFrame(
            [dict(ETF=code, **value) for code, value in latest.items()]
        )
        self.log_pane.value = self._tail(
            self.controller.log_path(self.trading_day)
        )

    def stop_runtime(self) -> None:
        if self.callback is not None and self.callback.running:
            self.callback.stop()

    def _start(self, _event) -> None:
        selected = tuple(self.monitor_etfs.value)
        if not os.getenv("SZ_REDIS_HOST"):
            self._set_message(
                "当前Panel进程未设置SZ_REDIS_HOST，暂不能启动内网采集。",
                "warning",
            )
            return
        paths = {}
        missing = []
        for code in selected:
            try:
                path = self.pcf.repository.find(code, self.trading_day)
            except Exception:
                path = None
            if path is None:
                missing.append(code)
            else:
                paths[code] = str(path)
        if missing:
            self._set_message(
                "缺少已校验的当日PCF：{}".format(", ".join(missing)),
                "warning",
            )
            return
        try:
            pid = self.controller.start(
                MarketMonitorJob(
                    trade_date=self.trading_day,
                    etf_codes=selected,
                    pcf_paths=paths,
                    interval=float(self.interval.value),
                    redis_code_suffix=self.redis_suffix.value.strip() or ".SZ",
                )
            )
            self._set_message(
                "后台采集已启动，进程号{}。".format(pid),
                "success",
            )
            self.refresh()
        except Exception as exc:
            self._set_message("启动失败：{}".format(exc), "error")

    def _stop(self, _event) -> None:
        self.controller.request_stop(self.trading_day)
        self._set_message(
            "已发送停止请求，采集器会在当前轮结束后退出。",
            "warning",
        )
        self.refresh()

    def _refresh_clicked(self, _event) -> None:
        self.refresh()
        if self.on_observations_changed is not None:
            self.on_observations_changed()

    def _monitor_selection_changed(self, _event) -> None:
        self.refresh()

    def _auto_refresh_changed(self, event) -> None:
        if event.new:
            if self.callback is None:
                self.callback = pn.state.add_periodic_callback(
                    self._periodic_refresh,
                    period=3000,
                    start=True,
                )
            elif not self.callback.running:
                self.callback.start()
        elif self.callback is not None and self.callback.running:
            self.callback.stop()

    def _periodic_refresh(self) -> None:
        self.refresh()
        if self.on_observations_changed is not None:
            self.on_observations_changed()

    @staticmethod
    def _tail(path: Path, lines: int = 80) -> str:
        if not path.exists():
            return ""
        try:
            values = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(values[-lines:])

    def _set_message(self, text: str, kind: str) -> None:
        colors = {
            "success": ("#2F6B4F", "#eef7f2"),
            "warning": ("#8B5E34", "#fff8ec"),
            "error": ("#A33A2B", "#fff1ef"),
        }
        foreground, background = colors.get(kind, ("#374147", "#f4f7f7"))
        self.message.object = (
            '<div style="border-left:3px solid {fg};background:{bg};'
            'padding:8px 10px;color:{fg}">{text}</div>'
        ).format(fg=foreground, bg=background, text=text)
