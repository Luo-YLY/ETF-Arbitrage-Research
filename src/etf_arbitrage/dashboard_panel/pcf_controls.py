"""Reusable Panel controls for locating, downloading, and uploading PCFs."""

from __future__ import annotations

from datetime import date, datetime
import json
import os
from pathlib import Path
from typing import Callable, Dict, Optional, Union

import pandas as pd
import panel as pn

from etf_arbitrage.data import (
    ETF_PROFILES,
    PCFRepository,
    etf_search_options,
    extract_etf_code,
    format_etf_search_option,
    load_pcf_source_templates,
    validate_executable_pcf,
)
from etf_arbitrage.domain import Exchange, infer_etf_exchange


class PanelPCFControls:
    """PCF preparation surface shared by both Panel research pages."""

    def __init__(
        self,
        project_root: Union[Path, str],
        default_etf: str = "159915",
        default_date: Optional[date] = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.repository = PCFRepository(self.project_root / "data" / "pcf")
        self.local_sources_path = (
            self.project_root / "config" / "pcf_sources.local.json"
        )
        self.example_sources_path = (
            self.project_root / "config" / "pcf_sources.example.json"
        )
        self._callbacks: list[Callable[[], None]] = []
        self._custom_path: Optional[Path] = None

        self.etf_select = pn.widgets.AutocompleteInput(
            label="ETF代码或名称",
            options=etf_search_options(self._local_etf_codes()),
            value=format_etf_search_option(default_etf),
            placeholder="输入6位代码、名称，或从建议中选择",
            restrict=False,
            case_sensitive=False,
            search_strategy="includes",
            min_characters=1,
        )
        self.date_picker = pn.widgets.DatePicker(
            label="交易日",
            value=default_date or self._latest_local_day(default_etf),
        )
        self.source_mode = pn.widgets.Select(
            label="PCF来源",
            options={
                "本地文件": "LOCAL_FILE",
                "官方下载": "OFFICIAL_DOWNLOAD",
                "上传文件": "UPLOAD",
            },
            value="LOCAL_FILE",
        )
        self.url_input = pn.widgets.TextInput(
            label="官方PCF地址",
            placeholder="深交所下载页、XML直链或带日期占位符的模板",
        )
        self.local_path_input = pn.widgets.TextInput(
            label="本地PCF路径",
            placeholder="选择本地文件时可手工指定路径",
        )
        self.upload = pn.widgets.FileInput(
            label="上传PCF XML、JSON或ZIP",
            accept=".xml,.json,.zip",
            multiple=False,
        )
        self.download_button = pn.widgets.Button(
            label="下载并校验",
            icon="download",
            color="primary",
        )
        self.save_url_button = pn.widgets.Button(
            label="保存本机地址",
            icon="device-floppy",
        )
        self.import_button = pn.widgets.Button(
            label="导入并校验",
            icon="file-upload",
            color="primary",
        )
        self.reload_button = pn.widgets.Button(
            label="重新加载",
            icon="refresh",
            color="primary",
        )
        self.refresh_button = pn.widgets.Button(
            label="刷新PCF状态",
            icon="refresh",
        )
        self.message = pn.pane.HTML("", sizing_mode="stretch_width")
        self.status_table = pn.widgets.Tabulator(
            pd.DataFrame(),
            show_index=False,
            disabled=True,
            height=190,
            sizing_mode="stretch_width",
        )

        self.download_controls = pn.Column(
            self.url_input,
            pn.Row(self.download_button, self.save_url_button),
            sizing_mode="stretch_width",
            visible=False,
        )
        self.upload_controls = pn.Column(
            self.upload,
            self.import_button,
            sizing_mode="stretch_width",
            visible=False,
        )
        self.local_controls = pn.Column(
            self.local_path_input,
            self.reload_button,
            sizing_mode="stretch_width",
        )

        self.etf_select.param.watch(self._selection_changed, "value")
        self.etf_select.param.watch(
            self._search_input_changed,
            "value_input",
        )
        self.date_picker.param.watch(self._selection_changed, "value")
        self.source_mode.param.watch(self._source_mode_changed, "value")
        self.download_button.on_click(self._download)
        self.save_url_button.on_click(self._save_url)
        self.import_button.on_click(self._import_upload)
        self.reload_button.on_click(self._reload_local)
        self.refresh_button.on_click(self._refresh_clicked)
        self._source_mode_changed(None)
        self.refresh()

    @property
    def etf_code(self) -> str:
        return extract_etf_code(str(self.etf_select.value))

    @property
    def trading_date(self) -> date:
        value = self.date_picker.value
        if isinstance(value, datetime):
            return value.date()
        return value

    @property
    def trading_day(self) -> str:
        return self.trading_date.strftime("%Y%m%d")

    @property
    def selected_path(self) -> Optional[Path]:
        if self._custom_path is not None and self._custom_path.exists():
            try:
                self.repository.validate(
                    self._custom_path,
                    self.etf_code,
                    self.trading_day,
                )
                return self._custom_path
            except Exception:
                self._custom_path = None
        try:
            return self.repository.find(self.etf_code, self.trading_day)
        except Exception:
            return None

    def on_change(self, callback: Callable[[], None]) -> None:
        self._callbacks.append(callback)

    def view(self):
        return pn.Column(
            pn.Row(self.etf_select, self.date_picker),
            self.source_mode,
            self.local_controls,
            self.download_controls,
            self.upload_controls,
            self.message,
            self.status_table,
            self.refresh_button,
            sizing_mode="stretch_width",
        )

    def refresh(self) -> None:
        try:
            etf_code = self.etf_code
        except ValueError as exc:
            self.status_table.value = pd.DataFrame(
                [{"状态": "等待输入", "提示": str(exc)}]
            )
            self.local_path_input.value = ""
            return
        self.etf_select.options = etf_search_options(self._local_etf_codes())
        templates = self._source_templates()
        self.url_input.value = templates.get(etf_code, "")
        is_sse = infer_etf_exchange(etf_code) == Exchange.SSE
        if is_sse and not self.url_input.value:
            self.url_input.placeholder = (
                "沪市无需填写：直接使用上交所官方PCF查询接口"
            )
            self.save_url_button.disabled = True
        else:
            self.url_input.placeholder = (
                "深交所下载页、XML直链或带日期占位符的模板"
            )
            self.save_url_button.disabled = False
        path = self.selected_path
        self.local_path_input.value = str(path) if path is not None else ""
        rows = []
        if path is None:
            rows.append(
                {
                    "ETF": self.etf_code,
                    "交易日": self.trading_day,
                    "状态": "缺失",
                    "本地文件": "",
                }
            )
        else:
            try:
                document, report = validate_executable_pcf(
                    path,
                    self.etf_code,
                    self.trading_date,
                )
                rows.append(
                    {
                        "ETF": document.etf_code,
                        "交易日": document.trading_day,
                        "状态": "已校验" if report.valid else "校验失败",
                        "最小申赎单位": document.creation_redemption_unit,
                        "成分股": len(document.components),
                        "预估现金差额": document.estimate_cash_component,
                        "本地文件": str(path.relative_to(self.project_root))
                        if path.is_relative_to(self.project_root)
                        else str(path),
                    }
                )
            except Exception as exc:
                rows.append(
                    {
                        "ETF": self.etf_code,
                        "交易日": self.trading_day,
                        "状态": "校验失败",
                        "错误": str(exc),
                        "本地文件": str(path),
                    }
                )
        self.status_table.value = pd.DataFrame(rows)

    def _selection_changed(self, _event) -> None:
        self._custom_path = None
        self.refresh()
        self._notify_callbacks()

    def _search_input_changed(self, event) -> None:
        try:
            extract_etf_code(str(event.new))
        except ValueError:
            return
        if self.etf_select.value != event.new:
            self.etf_select.value = str(event.new)

    def _source_mode_changed(self, _event) -> None:
        mode = self.source_mode.value
        self.local_controls.visible = mode == "LOCAL_FILE"
        self.download_controls.visible = mode == "OFFICIAL_DOWNLOAD"
        self.upload_controls.visible = mode == "UPLOAD"

    def _download(self, _event) -> None:
        try:
            path = self.repository.download(
                self.url_input.value,
                self.etf_code,
                self.trading_day,
            )
            self._custom_path = None
            self._set_message(
                "PCF已下载并校验：{}".format(
                    path.relative_to(self.project_root)
                ),
                "success",
            )
            self.refresh()
            self._notify_callbacks()
        except Exception as exc:
            self._set_message("PCF下载失败：{}".format(exc), "error")

    def _save_url(self, _event) -> None:
        try:
            payload: Dict[str, str] = {}
            if self.local_sources_path.exists():
                current = json.loads(
                    self.local_sources_path.read_text(encoding="utf-8")
                )
                if isinstance(current, dict):
                    payload.update(
                        {str(key): str(value) for key, value in current.items()}
                    )
            payload[self.etf_code] = self.url_input.value.strip()
            self.local_sources_path.parent.mkdir(parents=True, exist_ok=True)
            self.local_sources_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._set_message("PCF下载地址已保存到本机配置。", "success")
        except Exception as exc:
            self._set_message("保存失败：{}".format(exc), "error")

    def _import_upload(self, _event) -> None:
        if not self.upload.value:
            self._set_message("请先选择PCF XML、JSON或ZIP文件。", "warning")
            return
        try:
            path = self.repository.save(
                self.upload.value,
                self.etf_code,
                self.trading_day,
            )
            self._custom_path = None
            self._set_message(
                "PCF已导入并校验：{}".format(
                    path.relative_to(self.project_root)
                ),
                "success",
            )
            self.refresh()
            self._notify_callbacks()
        except Exception as exc:
            self._set_message("PCF导入失败：{}".format(exc), "error")

    def _reload_local(self, _event) -> None:
        try:
            candidate = Path(self.local_path_input.value).expanduser().resolve()
            document, report = validate_executable_pcf(
                candidate,
                self.etf_code,
                self.trading_date,
            )
            if not report.valid:
                raise ValueError(", ".join(report.errors))
            self._custom_path = candidate
            self._set_message(
                "PCF已重新加载：{}，成分股{}只。".format(
                    document.etf_code,
                    len(document.components),
                ),
                "success",
            )
            self.refresh()
            self._notify_callbacks()
        except Exception as exc:
            self._set_message("PCF重新加载失败：{}".format(exc), "error")

    def _refresh_clicked(self, _event) -> None:
        self.refresh()
        self._notify_callbacks()

    def _source_templates(self) -> Dict[str, str]:
        values = load_pcf_source_templates(self.example_sources_path)
        if self.local_sources_path.exists():
            payload = json.loads(
                self.local_sources_path.read_text(encoding="utf-8")
            )
            if isinstance(payload, dict):
                values.update(
                    {
                        str(code): str(url)
                        for code, url in payload.items()
                        if url
                    }
                )
        for code in ETF_PROFILES:
            value = os.getenv("ETF_PCF_URL_{}".format(code), "").strip()
            if value:
                values[code] = value
        return values

    def _latest_local_day(self, etf_code: str) -> date:
        paths = sorted(
            [
                *(self.project_root / "data" / "pcf").glob(
                    "*/pcf_{}_*.xml".format(etf_code)
                ),
                *(self.project_root / "data" / "pcf").glob(
                    "*/pcf_{}_*.json".format(etf_code)
                ),
            ],
            reverse=True,
        )
        for path in paths:
            try:
                return datetime.strptime(path.parent.name, "%Y%m%d").date()
            except ValueError:
                continue
        return date.today()

    def _local_etf_codes(self) -> list[str]:
        codes = []
        root = self.project_root / "data" / "pcf"
        for path in [*root.glob("*/*.xml"), *root.glob("*/*.json")]:
            parts = path.stem.split("_")
            if len(parts) >= 3 and len(parts[1]) == 6 and parts[1].isdigit():
                codes.append(parts[1])
        return sorted(set(codes))

    def _notify_callbacks(self) -> None:
        for callback in self._callbacks:
            callback()

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
