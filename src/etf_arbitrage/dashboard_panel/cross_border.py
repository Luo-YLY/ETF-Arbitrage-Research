"""Independent Panel console for mainland-listed cross-border ETF research."""

from __future__ import annotations

from datetime import date
from html import escape
import os
from pathlib import Path
from typing import Optional, Union

import pandas as pd
import panel as pn

from etf_arbitrage.data import PCFDocument, SubstituteFlag
from etf_arbitrage.market_data import (
    CrossBorderSnapshotInterface,
    CrossBorderSnapshotSourceMode,
    PendingCrossBorderMarketDataSource,
    cross_border_hk_components,
    virtual_subscription_cash_component,
)

from .pcf_controls import PanelPCFControls


pn.extension("tabulator", notifications=True, sizing_mode="stretch_width")

ACCENT = "#0F5D66"
CONSOLE_CSS = """
.cb-status-strip {display:grid;grid-template-columns:repeat(4,minmax(125px,1fr));gap:10px;width:100%;}
.cb-status-cell {min-height:78px;padding:12px 14px;border:1px solid #d8dee1;border-top:3px solid var(--cb-accent,#0f5d66);border-radius:4px;background:#fff;}
.cb-status-label {color:#647078;font-size:12px;}
.cb-status-value {color:#202a2e;font-size:17px;font-weight:600;margin-top:5px;}
.cb-note {padding:11px 13px;border-left:4px solid #0f5d66;background:#f7fafb;color:#263238;overflow-wrap:anywhere;}
@media (max-width:850px){.cb-status-strip{grid-template-columns:repeat(2,minmax(125px,1fr));}}
"""


class CrossBorderPanelDashboard:
    """Describe and audit the official-PCF plus intranet-Redis research path."""

    def __init__(
        self,
        project_root: Union[Path, str],
        default_date: Optional[date] = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.pcf_controls = PanelPCFControls(
            self.project_root,
            default_etf="159920",
            default_date=default_date,
        )
        self.pcf_controls.source_mode.value = "OFFICIAL_DOWNLOAD"
        self.pcf_controls.date_picker.name = "PCF交易日期"
        self.pcf_controls.download_controls.objects = [
            self.pcf_controls.url_input,
            self.pcf_controls.download_button,
            self.pcf_controls.save_url_button,
        ]
        self.pcf_controls.on_change(self.refresh)

        self.document: Optional[PCFDocument] = None
        self.document_path: Optional[Path] = None
        self.snapshot_source_mode = pn.widgets.Select(
            label="跨市场行情来源",
            options={
                "内网Redis实时行情（主路径）": CrossBorderSnapshotSourceMode.REDIS,
                "Redis采集后的标准化JSONL回放": CrossBorderSnapshotSourceMode.JSONL,
            },
            value=CrossBorderSnapshotSourceMode.REDIS,
        )
        self.snapshot_path = pn.widgets.TextInput(
            label="标准化观察文件",
            value="tmp/recorded/{trade_date}/{etf_code}_observations.jsonl",
            disabled=True,
        )
        self.redis_contract = pn.widgets.TextInput(
            label="Redis读取契约",
            value="Hash=YYYYMMDD；ETF=159920.SZ；港股成分=xxxxx.HK",
            disabled=True,
        )

        self.message = pn.pane.HTML("", sizing_mode="stretch_width")
        self.status_strip = pn.pane.HTML(
            "", sizing_mode="stretch_width", stylesheets=[CONSOLE_CSS]
        )
        self.scope_status = pn.pane.HTML("", sizing_mode="stretch_width")
        self.market_status = pn.pane.HTML("", sizing_mode="stretch_width")
        self.research_status = pn.pane.HTML("", sizing_mode="stretch_width")
        self.sidebar_pcf_status = pn.pane.HTML(
            "", sizing_mode="stretch_width", stylesheets=[CONSOLE_CSS]
        )
        self.sidebar_pcf_layout = pn.Column(
            self.pcf_controls.etf_select,
            self.pcf_controls.source_mode,
            self.pcf_controls.local_controls,
            self.pcf_controls.download_controls,
            self.pcf_controls.upload_controls,
            self.pcf_controls.date_picker,
            self.pcf_controls.message,
            self.sidebar_pcf_status,
            self.pcf_controls.refresh_button,
            sizing_mode="stretch_width",
        )

        self.scope_table = _table(300)
        self.roadmap_table = _table(230)
        self.pcf_header_table = _table(370)
        self.settlement_table = _table(230)
        self.components_table = _table(520)
        self.contract_table = _table(360)
        self.audit_table = _table(340)
        self.snapshot_source_mode.param.watch(
            lambda _event: self._update_market_views(), "value"
        )
        self.refresh()

    def sidebar_controls(self):
        return pn.Column(
            pn.pane.Markdown("### 跨境ETF与官方PCF"),
            self.sidebar_pcf_layout,
            pn.layout.Divider(),
            pn.pane.Markdown("### 行情路径"),
            self.snapshot_source_mode,
            sizing_mode="stretch_width",
        )

    def template(self):
        return pn.template.FastListTemplate(
            title="港股通跨境ETF研究控制台",
            site="ETF Arbitrage",
            accent_base_color=ACCENT,
            header_background="#202A2E",
            sidebar=[self.sidebar_controls()],
            main=[
                pn.Tabs(
                    ("研究边界", self.scope_view()),
                    ("官方PCF与申赎", self.pcf_view()),
                    ("跨市行情接口", self.market_interface_view()),
                    ("IOPV研究口径", self.research_view()),
                    ("数据质量与审计", self.audit_view()),
                    dynamic=True,
                    sizing_mode="stretch_width",
                )
            ],
            sidebar_width=350,
            main_layout=None,
        )

    def scope_view(self):
        return pn.Column(
            pn.pane.Markdown("## 当前主线：境内上市港股通ETF"),
            self.status_strip,
            self.scope_status,
            pn.pane.Markdown("#### 本阶段研究对象"),
            self.scope_table,
            pn.pane.Markdown("#### 扩展顺序"),
            self.roadmap_table,
            sizing_mode="stretch_width",
        )

    def pcf_view(self):
        return pn.Column(
            pn.pane.Markdown("## 深交所官方PCF与现金申赎结构"),
            self.message,
            self.pcf_header_table,
            self.settlement_table,
            pn.pane.Markdown("#### 港股参考篮子"),
            self.components_table,
            sizing_mode="stretch_width",
        )

    def market_interface_view(self):
        return pn.Column(
            pn.pane.Markdown("## 境内ETF + 港股篮子 + HKD/CNY"),
            self.market_status,
            pn.Row(self.redis_contract, self.snapshot_path),
            self.contract_table,
            sizing_mode="stretch_width",
        )

    def research_view(self):
        return pn.Column(
            pn.pane.Markdown(
                "## IOPV与折溢价研究口径\n\n"
                "申购方向使用港股成分卖一与HKD/CNY卖价，和159920买一比较；"
                "赎回方向使用港股成分买一与HKD/CNY买价，和159920卖一比较。"
            ),
            self.research_status,
            sizing_mode="stretch_width",
        )

    def audit_view(self):
        return pn.Column(
            pn.pane.Markdown("## 来源与完成度"),
            self.audit_table,
            sizing_mode="stretch_width",
        )

    def refresh(self) -> None:
        self.document = None
        self.document_path = self.pcf_controls.selected_path
        if self.document_path is not None:
            try:
                self.document = self.pcf_controls.repository.validate(
                    self.document_path,
                    self.pcf_controls.etf_code,
                    self.pcf_controls.trading_day,
                )
            except Exception as exc:
                self.message.object = _message_box(
                    "PCF校验失败：{}".format(exc), "error"
                )
        self._update_views()

    def _update_views(self) -> None:
        document = self.document
        hk_components = cross_border_hk_components(document) if document else ()
        redis_configured = bool(os.getenv("SZ_REDIS_HOST"))
        self.status_strip.object = _status_strip(
            (
                ("交易标的", "{} [SZ]".format(self.pcf_controls.etf_code), ACCENT),
                ("官方PCF", "已校验" if document else "待准备", "#2F6B4F" if document else "#8B5E34"),
                ("港股篮子", "{}只".format(len(hk_components)) if document else "待读取", ACCENT),
                ("Redis环境", "已配置" if redis_configured else "待内网配置", "#2F6B4F" if redis_configured else "#8B5E34"),
            )
        )
        self.scope_status.object = _message_box(
            "主交易腿是深交所上市ETF，价值腿由官方PCF中的港股成分构成。"
            "行情统一改由内网Redis提供；当前设备不再尝试公共第三方下载。",
            "info",
        )
        self.scope_table.value = pd.DataFrame(
            [
                {"模块": "二级市场交易腿", "对象": "159920.SZ及同类ETF", "输入": "Redis Bid/Ask与成交"},
                {"模块": "一级市场规则", "对象": "深交所/基金管理人官方PCF", "输入": "申赎单位、现金替代与开放状态"},
                {"模块": "境外价值腿", "对象": "PCF中的HKEX成分", "输入": "Redis港股Bid/Ask"},
                {"模块": "汇率腿", "对象": "HKD/CNY", "输入": "内网行情或正式授权源"},
                {"模块": "研究边界", "对象": "只识别/纸面模拟", "输入": "完整时钟与数据质量阻断"},
            ]
        )
        self.roadmap_table.value = pd.DataFrame(
            [
                {"阶段": "L1 当前", "范围": "159920 + 官方PCF + Redis最新价", "交付": "字段核验、连续记录、均值回复"},
                {"阶段": "L2", "范围": "港股通资格 + 双边盘口 + HKD/CNY", "交付": "方向性IOPV与可交易覆盖率"},
                {"阶段": "L3", "范围": "沪深港通同类ETF", "交付": "横截面对比和统一成本模型"},
                {"阶段": "L4", "范围": "QDII现金申赎状态机", "交付": "可执行性漏斗与纸面套利"},
            ]
        )
        self._update_pcf_views()
        self._update_market_views()

    def _update_pcf_views(self) -> None:
        document = self.document
        if document is None:
            self.sidebar_pcf_status.object = _note("PCF待准备：下载或导入后显示校验摘要。")
            self.pcf_header_table.value = pd.DataFrame(
                [{"字段": "状态", "值": "缺失"}, {"字段": "下一步", "值": "下载并校验官方PCF"}]
            )
            self.settlement_table.value = pd.DataFrame()
            self.components_table.value = pd.DataFrame()
            return
        hk_components = cross_border_hk_components(document)
        virtual_cash = virtual_subscription_cash_component(document)
        self.sidebar_pcf_status.object = _note(
            "PCF已校验：{}，港股成分{}只，最小申赎单位{:,}。".format(
                document.trading_day,
                len(hk_components),
                document.creation_redemption_unit,
            )
        )
        self.pcf_header_table.value = pd.DataFrame(
            [
                {"字段": "基金", "值": "{} ({})".format(document.symbol, document.etf_code)},
                {"字段": "交易日", "值": document.trading_day},
                {"字段": "最小申赎单位", "值": document.creation_redemption_unit},
                {"字段": "预估现金部分(人民币元)", "值": document.estimate_cash_component},
                {"字段": "申购/赎回开放", "值": "{}/{}".format("是" if document.creation_allowed else "否", "是" if document.redemption_allowed else "否")},
                {"字段": "PCF文件", "值": str(self.document_path or "")},
            ]
        )
        self.settlement_table.value = pd.DataFrame(
            [
                {"层": "虚拟清算证券", "代码": virtual_cash.stock_code if virtual_cash else "未发现", "口径": "申赎现金，不进入IOPV篮子"},
                {"层": "HKEX参考篮子", "代码": "{}只成分".format(len(hk_components)), "口径": "数量 × 港股价 × HKD/CNY"},
            ]
        )
        flag_names = {
            SubstituteFlag.PROHIBITED: "禁止",
            SubstituteFlag.ALLOWED: "允许",
            SubstituteFlag.MANDATORY: "必须",
        }
        self.components_table.value = pd.DataFrame(
            [
                {
                    "代码": item.stock_code,
                    "名称": item.symbol,
                    "数量": item.component_share,
                    "现金替代": flag_names.get(item.substitute_flag, str(item.substitute_flag)),
                    "Redis字段": "{}.HK".format(item.stock_code),
                }
                for item in hk_components
            ]
        )

    def _update_market_views(self) -> None:
        mode = self.snapshot_source_mode.value
        if not isinstance(mode, CrossBorderSnapshotSourceMode):
            mode = CrossBorderSnapshotSourceMode(str(mode))
        interface = CrossBorderSnapshotInterface(etf_code=self.pcf_controls.etf_code)
        source = PendingCrossBorderMarketDataSource(interface, mode)
        health = source.health()
        redis_configured = bool(os.getenv("SZ_REDIS_HOST"))
        if mode == CrossBorderSnapshotSourceMode.REDIS:
            text = (
                "Redis已设为唯一实时主路径。当前进程{}SZ_REDIS_HOST；"
                "正式计算前还需在内网设备验证港股代码后缀、Bid/Ask、时间戳和HKD/CNY字段。"
            ).format("已配置" if redis_configured else "未配置")
        else:
            text = "JSONL只回放由内网Redis采集生成的标准化分钟观察，不负责联网下载。"
        self.market_status.object = _message_box(text, "warning")
        self.contract_table.value = pd.DataFrame(
            [
                {"项目": "实时行情", "约定": "Redis Hash=YYYYMMDD", "状态": health.status},
                {"项目": "境内ETF", "约定": "159920.SZ", "状态": "待内网字段核验"},
                {"项目": "港股成分", "约定": "PCF代码 + .HK", "状态": "待内网字段核验"},
                {"项目": "必要行情字段", "约定": "closepx / bidpx1 / askpx1 / cdate / ctime", "状态": "缺一则INDICATIVE"},
                {"项目": "汇率", "约定": "HKD/CNY bid / ask / timestamp", "状态": "待确认Redis键"},
                {"项目": "历史研究", "约定": interface.jsonl_path_template, "状态": "由Redis采集器生成"},
            ]
        )
        self.research_status.object = _message_box(
            "在ETF、全部港股成分、HKD/CNY和官方PCF完成同一时钟对齐前，"
            "结果保持INDICATIVE；缺少完整双边盘口时不宣称可执行套利。",
            "warning",
        )
        self.audit_table.value = pd.DataFrame(
            [
                {"数据/规则": "159920申购赎回清单", "来源": "深交所官方PCF", "状态": "已校验" if self.document else "待下载"},
                {"数据/规则": "境内ETF最新价/盘口", "来源": "内网Redis", "状态": "主路径，待设备验证"},
                {"数据/规则": "港股成分最新价/盘口", "来源": "内网Redis", "状态": "已确认有数据，待字段验收"},
                {"数据/规则": "HKD/CNY", "来源": "内网Redis或正式授权源", "状态": "键与字段待确认"},
                {"数据/规则": "历史观察", "来源": "Redis采集后的本地JSONL", "状态": "不再使用公共下载接口"},
                {"数据/规则": "港股通资格", "来源": "交易所官方名单", "状态": "接口待接入"},
            ]
        )


def build_cross_border_panel_dashboard(project_root: Union[Path, str]):
    from .cross_border_console import build_cross_border_console_dashboard

    return build_cross_border_console_dashboard(project_root)


def _table(height: int) -> pn.widgets.Tabulator:
    return pn.widgets.Tabulator(
        pd.DataFrame(),
        show_index=False,
        disabled=True,
        height=height,
        pagination="local",
        page_size=15,
        sizing_mode="stretch_width",
    )


def _message_box(text: str, level: str = "info") -> str:
    colors = {"info": "#0f5d66", "warning": "#9a6700", "error": "#b42318"}
    color = colors.get(level, colors["info"])
    return (
        '<div class="cb-note" style="border-left-color:{}">{}</div>'.format(
            color, escape(text)
        )
    )


def _note(text: str) -> str:
    return '<div class="cb-note">{}</div>'.format(escape(text))


def _status_strip(items) -> str:
    cells = []
    for label, value, color in items:
        cells.append(
            '<div class="cb-status-cell" style="--cb-accent:{}">'
            '<div class="cb-status-label">{}</div>'
            '<div class="cb-status-value">{}</div></div>'.format(
                color, escape(str(label)), escape(str(value))
            )
        )
    return '<div class="cb-status-strip">{}</div>'.format("".join(cells))
