"""Panel controls for the real Redis collection and PCF preparation workflow."""

from __future__ import annotations

from datetime import date, datetime
import os
from pathlib import Path
from typing import Callable, Optional, Union

import pandas as pd
import panel as pn

from etf_arbitrage.data import (
    ETF_PROFILES,
    JsonlSnapshotStore,
    etf_profile,
    etf_search_options,
    extract_etf_code,
)
from etf_arbitrage.domain import vendor_symbol
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

STATUS_LABELS = {
    "starting": "启动中",
    "waiting": "等待开盘",
    "running": "采集中",
    "retry_wait": "异常后等待重启",
    "stopped": "已停止",
    "completed": "已完成",
    "error": "错误终止",
    "invalid_state": "状态文件异常",
}


class MeanReversionOperations:
    """Operational surface for the real-quote mean-reversion page."""

    def __init__(
        self,
        project_root: Union[Path, str],
        on_observations_changed: Optional[Callable[[], None]] = None,
        pcf_controls: Optional[PanelPCFControls] = None,
        default_etfs: Optional[tuple[str, ...]] = None,
        capture_only: bool = False,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.controller = MarketMonitorController(self.project_root)
        self.schedule = SZSEMarketSchedule()
        self.on_observations_changed = on_observations_changed
        self.capture_only = bool(capture_only)
        self.callback = None

        resolved_defaults = tuple(default_etfs or ("159915",))
        self.pcf = pcf_controls or PanelPCFControls(
            self.project_root,
            default_etf=resolved_defaults[0],
            default_date=date.today(),
        )
        self.monitor_etfs = pn.widgets.MultiChoice(
            label="监控ETF",
            options=list(ETF_PROFILES),
            value=list(resolved_defaults),
            placeholder="已加入的监控标的",
        )
        self.monitor_search = pn.widgets.AutocompleteInput(
            label="添加ETF代码或名称",
            options=etf_search_options(),
            value="",
            placeholder="可输入任意6位ETF代码",
            restrict=False,
            case_sensitive=False,
            search_strategy="includes",
            min_characters=1,
        )
        self.add_monitor_button = pn.widgets.Button(
            label="加入监控",
            icon="plus",
            color="primary",
        )
        self.interval = pn.widgets.FloatInput(
            label="采集间隔（秒）",
            value=3.0,
            start=1.0,
            end=30.0,
            step=1.0,
        )
        self.auto_restart = pn.widgets.Toggle(
            label="异常自动重启",
            value=True,
            icon="refresh",
        )
        self.restart_delay = pn.widgets.FloatInput(
            label="首次重试等待（秒）",
            value=5.0,
            start=1.0,
            end=60.0,
            step=1.0,
        )
        self.redis_suffix = pn.widgets.TextInput(
            label="未知交易所的默认Redis后缀",
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
            label="自动刷新行情与任务",
            value=True,
            icon="refresh",
        )
        self.message = pn.pane.HTML("", sizing_mode="stretch_width")
        self.restart_status = pn.pane.HTML("", sizing_mode="stretch_width")
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
        self.latest_quotes_table = pn.widgets.Tabulator(
            pd.DataFrame(),
            show_index=False,
            disabled=True,
            height=520,
            pagination="local",
            page_size=25,
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
        self.add_monitor_button.on_click(self._add_monitor_etf)
        self.pcf.on_change(self.refresh)
        self.start_button.on_click(self._start)
        self.stop_button.on_click(self._stop)
        self.refresh_button.on_click(self._refresh_clicked)
        self.auto_refresh.param.watch(self._auto_refresh_changed, "value")
        self.refresh()
        pn.state.onload(self.start_runtime)

    @property
    def trading_day(self) -> str:
        return self.pcf.trading_day

    def view(self):
        if self.capture_only:
            return pn.Column(
                pn.pane.Markdown("## 港股通ETF与成分股最新价监控"),
                pn.pane.HTML(
                    '<div style="border-left:4px solid #8B5E34;background:#fff8ec;'
                    'padding:10px 12px;color:#5f4328">盘中仅保存ETF和PCF成分股最新价。'
                    '当前不计算IOPV、不显示Premium、不生成套利信号；估值状态固定为'
                    '<b>PENDING_FX</b>，收盘后由汇率回填程序生成独立派生结果。</div>',
                    sizing_mode="stretch_width",
                ),
                pn.Row(self.monitor_search, self.add_monitor_button),
                pn.Row(
                    self.monitor_etfs,
                    self.interval,
                    self.auto_restart,
                    self.restart_delay,
                ),
                pn.pane.Markdown(
                    "Redis代码规则：ETF使用交易所后缀（如`159920.SZ`）；"
                    "PCF中的港股成分固定映射为五位代码加大写`.HK`（如`00700.HK`）。"
                ),
                pn.Row(
                    self.start_button,
                    self.stop_button,
                    self.refresh_button,
                    self.auto_refresh,
                ),
                self.message,
                self.status_table,
                self.restart_status,
                pn.pane.Markdown("#### ETF采集状态"),
                self.latest_table,
                pn.pane.Markdown("#### ETF与PCF成分最新价"),
                self.latest_quotes_table,
                pn.pane.Markdown("#### 采集日志"),
                self.log_pane,
                sizing_mode="stretch_width",
            )
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
                        pn.Row(
                            self.monitor_search,
                            self.add_monitor_button,
                        ),
                        pn.Row(
                            self.monitor_etfs,
                            self.interval,
                            self.auto_restart,
                            self.restart_delay,
                        ),
                        self.redis_suffix,
                        pn.Row(
                            self.start_button,
                            self.stop_button,
                            self.refresh_button,
                            self.auto_refresh,
                        ),
                        self.message,
                        self.status_table,
                        self.restart_status,
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
                        "名称": etf_profile(code).name,
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
                        "名称": etf_profile(code).name,
                        "状态": "校验失败",
                        "错误": str(exc),
                    }
                )
        self.pcf_table.value = pd.DataFrame(rows)

        state = read_job_state(self.controller.state_path(self.trading_day))
        phase = self.schedule.phase(datetime.now())
        status = state.get("status", "not_started")
        self.status_table.value = pd.DataFrame(
            [
                {
                    "交易日": self.trading_day,
                    "当前时段": PHASE_LABELS.get(phase, phase),
                    "Redis配置": (
                        "已配置" if os.getenv("SZ_REDIS_HOST") else "未配置"
                    ),
                    "任务状态": STATUS_LABELS.get(status, "未启动"),
                    "进程号": state.get("pid", ""),
                    "累计轮询": state.get("polls", 0),
                    "自动重启": "已开启"
                    if state.get("auto_restart", self.auto_restart.value)
                    else "已关闭",
                    "重启次数": state.get("restart_count", 0),
                    "监控ETF": ",".join(state.get("etf_codes", selected)),
                }
            ]
        )
        self._render_restart_status(state)
        latest = state.get("latest", {})
        self.latest_table.value = pd.DataFrame(
            [dict(ETF=code, **value) for code, value in latest.items()]
        )
        if self.capture_only:
            self.latest_quotes_table.value = pd.DataFrame(
                self._latest_quote_rows(selected)
            )
        self.log_pane.value = self._tail(
            self.controller.log_path(self.trading_day)
        )

    def stop_runtime(self) -> None:
        if self.callback is not None and self.callback.running:
            self.callback.stop()

    def start_runtime(self) -> None:
        if not self.auto_refresh.value:
            return
        if self.callback is None:
            self.callback = pn.state.add_periodic_callback(
                self._periodic_refresh,
                period=3000,
                start=True,
            )
        elif not self.callback.running:
            self.callback.start()

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
                    auto_restart=bool(self.auto_restart.value),
                    restart_delay=float(self.restart_delay.value),
                    capture_only=self.capture_only,
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
            "已发送停止请求；若任务正在等待自动重启，本次重启也会被取消。",
            "warning",
        )
        self.refresh()

    def _refresh_clicked(self, _event) -> None:
        self.refresh()
        if self.on_observations_changed is not None:
            self.on_observations_changed()

    def _monitor_selection_changed(self, _event) -> None:
        self.refresh()

    def _add_monitor_etf(self, _event) -> None:
        try:
            code = extract_etf_code(
                self.monitor_search.value_input
                or self.monitor_search.value
            )
        except ValueError as exc:
            self._set_message(str(exc), "warning")
            return
        options = list(self.monitor_etfs.options)
        if code not in options:
            options.append(code)
            self.monitor_etfs.options = options
        selected = list(self.monitor_etfs.value)
        if code not in selected:
            selected.append(code)
            self.monitor_etfs.value = selected
        self.monitor_search.value = ""
        self._set_message("已将{}加入监控列表。".format(code), "success")

    def _auto_refresh_changed(self, event) -> None:
        if event.new:
            self.start_runtime()
        elif self.callback is not None and self.callback.running:
            self.callback.stop()

    def _periodic_refresh(self) -> None:
        self.refresh()
        if self.on_observations_changed is not None:
            self.on_observations_changed()

    def _latest_quote_rows(self, selected: list[str]) -> list[dict]:
        rows = []
        for code in selected:
            recording = (
                self.project_root
                / "tmp"
                / "recordings"
                / self.trading_day
                / "{}.jsonl".format(code)
            )
            try:
                path = self.pcf.repository.find(code, self.trading_day)
                if path is None or not recording.exists():
                    continue
                pcf = self.pcf.repository.validate(path, code, self.trading_day)
                snapshot = JsonlSnapshotStore(recording).latest_snapshot()
            except Exception:
                continue
            rows.append(
                {
                    "类别": "ETF",
                    "Redis代码": vendor_symbol(pcf.etf_id),
                    "名称": pcf.symbol,
                    "最新价": snapshot.etf_quote.last_price,
                    "前收盘": None,
                    "涨跌幅": None,
                    "行情时间": snapshot.etf_quote.timestamp,
                    "状态": "已记录",
                }
            )
            for component in pcf.components:
                if component.component_share <= 0:
                    continue
                quote = snapshot.stock_quotes.get(component.stock_code)
                previous_close = quote.previous_close if quote is not None else None
                last_price = quote.last_price if quote is not None else None
                change = (
                    last_price / previous_close - 1.0
                    if last_price is not None
                    and previous_close is not None
                    and previous_close > 0
                    else None
                )
                rows.append(
                    {
                        "类别": "成分股",
                        "Redis代码": vendor_symbol(component.instrument_id),
                        "名称": component.symbol,
                        "最新价": last_price,
                        "前收盘": previous_close,
                        "涨跌幅": change,
                        "行情时间": quote.timestamp if quote is not None else None,
                        "状态": "已记录" if last_price is not None else "行情缺失",
                    }
                )
        return rows

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

    def _render_restart_status(self, state) -> None:
        restart_count = int(state.get("restart_count", 0) or 0)
        last_error = state.get("last_error") or state.get("error")
        next_retry_at = state.get("next_retry_at")
        if state.get("status") == "retry_wait":
            text = "采集任务发生异常，系统正在守护运行"
            if next_retry_at:
                text += "；下次尝试：{}".format(next_retry_at)
            if last_error:
                text += "；最近错误：{}".format(last_error)
            kind = "warning"
        elif restart_count and last_error:
            text = "今日已自动重启 {} 次；最近错误：{}".format(
                restart_count,
                last_error,
            )
            kind = "warning"
        else:
            self.restart_status.object = ""
            return
        colors = {
            "warning": ("#8B5E34", "#fff8ec"),
            "error": ("#A33A2B", "#fff1ef"),
        }
        foreground, background = colors[kind]
        self.restart_status.object = (
            '<div style="border-left:3px solid {fg};background:{bg};'
            'padding:8px 10px;color:{fg}">{text}</div>'
        ).format(fg=foreground, bg=background, text=text)
