"""Panel controls for mainland multi-ETF executable snapshot collection."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import os
from pathlib import Path
from typing import Optional, Union

import pandas as pd
import panel as pn

from etf_arbitrage.data import (
    ETF_PROFILES,
    etf_profile,
    extract_etf_code,
    format_etf_search_option,
)
from etf_arbitrage.domain import Exchange
from etf_arbitrage.operations import (
    ExecutableMonitorController,
    ExecutableMonitorJob,
    SZSEMarketSchedule,
    read_job_state,
)

from .pcf_controls import PanelPCFControls


MAINLAND_EXECUTABLE_ETFS = tuple(
    code for code in ETF_PROFILES if code not in {"159920", "513660", "513600"}
)

STATUS_LABELS = {
    "not_started": "未启动",
    "starting": "启动中",
    "waiting": "等待开盘",
    "running": "采集中",
    "retry_wait": "异常后等待重启",
    "stopped": "已停止",
    "completed": "已完成",
    "error": "错误终止",
    "invalid_state": "状态文件异常",
}

PHASE_LABELS = {
    "closed": "休市",
    "before_open": "盘前等待",
    "morning_session": "上午连续竞价",
    "lunch_break": "午间等待",
    "afternoon_session": "下午连续竞价",
    "after_close": "已收盘",
}


class ExecutableMultiETFOperations:
    """Manage one detached mainland multi-ETF five-level collection job."""

    def __init__(
        self,
        project_root: Union[Path, str],
        pcf_controls: PanelPCFControls,
        default_etfs: Optional[tuple[str, ...]] = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.pcf = pcf_controls
        self.controller = ExecutableMonitorController(self.project_root)
        self.schedule = SZSEMarketSchedule()
        self.callback = None
        self._refreshing = False
        self._ready_selection_initialized = False
        defaults = [
            code
            for code in (default_etfs or ("159915",))
            if code in MAINLAND_EXECUTABLE_ETFS
        ] or [MAINLAND_EXECUTABLE_ETFS[0]]
        self._default_etfs = tuple(defaults)

        self.trading_date = pn.widgets.DatePicker(
            label="PCF与采集交易日",
            value=self.pcf.trading_date,
        )
        self.pcf_download_etfs = pn.widgets.MultiChoice(
            label="需要下载或校验PCF的ETF",
            options=list(MAINLAND_EXECUTABLE_ETFS),
            value=defaults,
            placeholder="选择需要准备当日PCF的境内ETF",
        )

        self.monitor_etfs = pn.widgets.MultiChoice(
            label="并行采集ETF（仅显示已下载且校验通过的当日PCF）",
            options=[],
            value=[],
            placeholder="请先下载并校验当日PCF",
        )
        self.monitor_search = pn.widgets.AutocompleteInput(
            label="添加ETF代码或名称到PCF清单",
            options=[
                format_etf_search_option(code) for code in MAINLAND_EXECUTABLE_ETFS
            ],
            value="",
            restrict=False,
            case_sensitive=False,
            search_strategy="includes",
            min_characters=1,
        )
        self.add_button = pn.widgets.Button(
            label="加入PCF清单",
            icon="plus",
            color="primary",
        )
        self.download_pcfs_button = pn.widgets.Button(
            label="一键下载并校验所选PCF",
            icon="download",
            color="primary",
        )
        self.ready_refresh_button = pn.widgets.Button(
            label="刷新已下载PCF",
            icon="refresh",
        )
        self.select_all_ready_button = pn.widgets.Button(
            label="选择全部已就绪ETF",
            icon="checks",
        )
        self.clear_monitor_button = pn.widgets.Button(
            label="清空采集选择",
            icon="x",
        )
        self.interval = pn.widgets.FloatInput(
            label="采集间隔（秒）",
            value=3.0,
            start=1.0,
            end=30.0,
            step=1.0,
        )
        self.max_workers = pn.widgets.IntInput(
            label="并行线程",
            value=4,
            start=1,
            end=16,
            step=1,
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
        self.start_button = pn.widgets.Button(
            label="启动境内多ETF五档采集",
            icon="player-play",
            color="primary",
        )
        self.stop_button = pn.widgets.Button(
            label="停止采集",
            icon="player-stop",
            color="warning",
        )
        self.refresh_button = pn.widgets.Button(
            label="刷新任务状态",
            icon="refresh",
        )
        self.auto_refresh = pn.widgets.Toggle(
            label="自动刷新状态",
            value=True,
            icon="refresh",
        )
        self.message = pn.pane.HTML("", sizing_mode="stretch_width")
        self.pcf_table = pn.widgets.Tabulator(
            pd.DataFrame(),
            show_index=False,
            disabled=True,
            height=190,
            sizing_mode="stretch_width",
        )
        self.status_table = pn.widgets.Tabulator(
            pd.DataFrame(),
            show_index=False,
            disabled=True,
            height=100,
            sizing_mode="stretch_width",
        )
        self.latest_table = pn.widgets.Tabulator(
            pd.DataFrame(),
            show_index=False,
            disabled=True,
            height=240,
            sizing_mode="stretch_width",
        )
        self.log_pane = pn.widgets.TextAreaInput(
            label="境内多ETF五档采集日志",
            value="",
            disabled=True,
            height=180,
            sizing_mode="stretch_width",
        )

        self.trading_date.param.watch(self._trading_date_changed, "value")
        self.monitor_etfs.param.watch(self._selection_changed, "value")
        self.add_button.on_click(self._add_etf)
        self.download_pcfs_button.on_click(self._download_selected_pcfs)
        self.ready_refresh_button.on_click(self._refresh_clicked)
        self.select_all_ready_button.on_click(self._select_all_ready)
        self.clear_monitor_button.on_click(self._clear_monitor)
        self.start_button.on_click(self._start)
        self.stop_button.on_click(self._stop)
        self.refresh_button.on_click(self._refresh_clicked)
        self.auto_refresh.param.watch(self._auto_refresh_changed, "value")
        self.pcf.on_change(self._pcf_controls_changed)
        self.refresh()
        pn.state.onload(self.start_runtime)

    @property
    def trading_day(self) -> str:
        return self.trading_date.value.strftime("%Y%m%d")

    def view(self):
        return pn.Card(
            pn.pane.Markdown(
                "该任务只接受沪深境内成分ETF。每只ETF独立读取当日PCF完整篮子、"
                "并行采集Redis五档并保存JSONL；含非沪深成分的PCF会被后台拒绝。"
            ),
            pn.pane.Markdown("#### 1. 批量准备当日PCF"),
            self.trading_date,
            pn.Row(self.monitor_search, self.add_button),
            self.pcf_download_etfs,
            pn.Row(self.download_pcfs_button, self.ready_refresh_button),
            self.pcf_table,
            pn.pane.Markdown("#### 2. 从已校验PCF中选择日内采集ETF"),
            self.monitor_etfs,
            pn.Row(self.select_all_ready_button, self.clear_monitor_button),
            pn.pane.Markdown("#### 3. 设置采集任务"),
            pn.Row(
                self.interval,
                self.max_workers,
                self.auto_restart,
                self.restart_delay,
            ),
            pn.Row(
                self.start_button,
                self.stop_button,
                self.refresh_button,
                self.auto_refresh,
            ),
            self.message,
            self.status_table,
            pn.pane.Markdown("#### 各ETF最新五档与质量状态"),
            self.latest_table,
            self.log_pane,
            title="境内多ETF日内五档采集",
            collapsed=False,
            collapsible=True,
            sizing_mode="stretch_width",
        )

    def refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            ready = self._available_mainland_pcfs()
            ready_codes = sorted(ready)
            selected = [
                code for code in self.monitor_etfs.value if code in ready
            ]
            if not self._ready_selection_initialized:
                selected = [code for code in self._default_etfs if code in ready]
                self._ready_selection_initialized = True
            self.monitor_etfs.param.update(
                options=ready_codes,
                value=selected,
            )

            preparation_codes = list(self.pcf_download_etfs.value)
            rows = []
            for code in dict.fromkeys([*preparation_codes, *ready_codes]):
                path = ready.get(code)
                rows.append(
                    {
                        "ETF": code,
                        "名称": etf_profile(code).name,
                        "PCF状态": "已下载并校验" if path is not None else "缺失",
                        "可选采集": "是" if path is not None else "否",
                        "本地文件": self._display_path(path),
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
                        "Redis配置": "已配置"
                        if os.getenv("SZ_REDIS_HOST")
                        else "未配置",
                        "任务状态": STATUS_LABELS.get(status, status),
                        "进程号": state.get("pid", ""),
                        "累计轮询": state.get("polls", 0),
                        "重启次数": state.get("restart_count", 0),
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
        finally:
            self._refreshing = False

    def start_runtime(self) -> None:
        if not self.auto_refresh.value:
            return
        if self.callback is None:
            self.callback = pn.state.add_periodic_callback(
                self.refresh,
                period=3_000,
                start=True,
            )
        elif not self.callback.running:
            self.callback.start()

    def stop_runtime(self) -> None:
        if self.callback is not None and self.callback.running:
            self.callback.stop()

    def _start(self, _event) -> None:
        selected = tuple(self.monitor_etfs.value)
        if not selected:
            self._set_message("请至少选择一只境内ETF。", "warning")
            return
        if not os.getenv("SZ_REDIS_HOST"):
            self._set_message(
                "启动Panel的同一终端未设置SZ_REDIS_HOST，暂不能启动后台五档采集。",
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
                "以下ETF的当日PCF尚未就绪，请先批量下载并校验：{}".format(
                    ", ".join(missing)
                ),
                "warning",
            )
            return
        try:
            pid = self.controller.start(
                ExecutableMonitorJob(
                    trade_date=self.trading_day,
                    etf_codes=selected,
                    pcf_paths=paths,
                    interval=float(self.interval.value),
                    number_of_book_levels=5,
                    max_workers=int(self.max_workers.value),
                    auto_restart=bool(self.auto_restart.value),
                    restart_delay=float(self.restart_delay.value),
                )
            )
            self._set_message(
                "境内多ETF五档后台任务已启动，进程号{}。".format(pid),
                "success",
            )
            self.refresh()
        except Exception as exc:
            self._set_message("启动失败：{}".format(exc), "error")

    def _stop(self, _event) -> None:
        self.controller.request_stop(self.trading_day)
        self._set_message("已发送停止请求。", "warning")
        self.refresh()

    def _refresh_clicked(self, _event) -> None:
        self.refresh()

    def _selection_changed(self, _event) -> None:
        self.refresh()

    def _trading_date_changed(self, event) -> None:
        self._ready_selection_initialized = False
        if self.pcf.date_picker.value != event.new:
            self.pcf.date_picker.value = event.new
        else:
            self.refresh()

    def _pcf_controls_changed(self) -> None:
        if self.trading_date.value != self.pcf.trading_date:
            self.trading_date.value = self.pcf.trading_date
            return
        self.refresh()

    def _download_selected_pcfs(self, _event) -> None:
        selected = tuple(dict.fromkeys(self.pcf_download_etfs.value))
        if not selected:
            self._set_message("请至少选择一只需要准备PCF的ETF。", "warning")
            return
        self.download_pcfs_button.disabled = True
        self.download_pcfs_button.loading = True
        successes: dict[str, Path] = {}
        failures: dict[str, str] = {}

        def download_one(code: str) -> Path:
            existing = self.pcf.repository.find(code, self.trading_day)
            if existing is not None:
                return existing
            return self.pcf.repository.download(
                self.pcf.download_url_for(code),
                code,
                self.trading_day,
            )

        try:
            workers = min(4, len(selected))
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(download_one, code): code for code in selected
                }
                for future in as_completed(futures):
                    code = futures[future]
                    try:
                        successes[code] = future.result()
                    except Exception as exc:
                        failures[code] = str(exc)
            self.refresh()
            selectable = set(self.monitor_etfs.options)
            newly_selected = list(self.monitor_etfs.value)
            for code in selected:
                if (
                    code in successes
                    and code in selectable
                    and code not in newly_selected
                ):
                    newly_selected.append(code)
            self.monitor_etfs.value = newly_selected
            if failures:
                details = "；".join(
                    "{}: {}".format(code, message)
                    for code, message in failures.items()
                )
                self._set_message(
                    "PCF批量处理完成：成功{}只，失败{}只。{}".format(
                        len(successes), len(failures), details
                    ),
                    "warning",
                )
            else:
                self._set_message(
                    "PCF批量下载、校验完成，共{}只；已加入日内采集选择。".format(
                        len(successes)
                    ),
                    "success",
                )
        finally:
            self.download_pcfs_button.loading = False
            self.download_pcfs_button.disabled = False

    def _select_all_ready(self, _event) -> None:
        self.monitor_etfs.value = list(self.monitor_etfs.options)

    def _clear_monitor(self, _event) -> None:
        self.monitor_etfs.value = []

    def _add_etf(self, _event) -> None:
        try:
            code = extract_etf_code(
                self.monitor_search.value_input or self.monitor_search.value
            )
        except ValueError as exc:
            self._set_message(str(exc), "warning")
            return
        if code in {"159920", "513660", "513600"}:
            self._set_message(
                "{}属于当前停用的港股/跨境范围，本任务不接收。".format(code),
                "warning",
            )
            return
        options = list(self.pcf_download_etfs.options)
        if code not in options:
            options.append(code)
            self.pcf_download_etfs.options = options
        selected = list(self.pcf_download_etfs.value)
        if code not in selected:
            selected.append(code)
            self.pcf_download_etfs.value = selected
        self.monitor_search.value = ""
        self._set_message("已将{}加入PCF准备清单。".format(code), "success")

    def _auto_refresh_changed(self, event) -> None:
        if event.new:
            self.start_runtime()
        else:
            self.stop_runtime()

    def _set_message(self, text: str, state: str) -> None:
        colors = {
            "success": ("#2F6B4F", "#eef7f2"),
            "warning": ("#8B5E34", "#fff8ec"),
            "error": ("#A33A2B", "#fff1ef"),
        }
        foreground, background = colors.get(state, ("#374147", "#f4f7f7"))
        self.message.object = (
            '<div style="border-left:3px solid {fg};background:{bg};'
            'padding:8px 10px;color:{fg}">{text}</div>'
        ).format(fg=foreground, bg=background, text=text)

    def _available_mainland_pcfs(self) -> dict[str, Path]:
        available = self.pcf.repository.available_for_day(self.trading_day)
        eligible: dict[str, Path] = {}
        mainland = {Exchange.SSE, Exchange.SZSE}
        for code, path in available.items():
            try:
                document = self.pcf.repository.validate(
                    path,
                    code,
                    self.trading_day,
                )
                if document.etf_id.exchange not in mainland:
                    continue
                if any(
                    component.component_share > 0
                    and component.instrument_id.exchange not in mainland
                    for component in document.components
                ):
                    continue
            except Exception:
                continue
            eligible[code] = path
        return eligible

    def _display_path(self, path: Optional[Path]) -> str:
        if path is None:
            return ""
        if path.is_relative_to(self.project_root):
            return str(path.relative_to(self.project_root))
        return str(path)

    @staticmethod
    def _tail(path: Path, maximum_lines: int = 30) -> str:
        if not path.exists():
            return ""
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            return "读取日志失败：{}".format(exc)
        return "\n".join(lines[-maximum_lines:])
