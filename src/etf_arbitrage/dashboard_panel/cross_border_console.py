"""Two-page Panel shell for mainland-listed cross-border ETF research."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

import panel as pn

from .cross_border import CrossBorderPanelDashboard
from .executable import PanelExecutableDashboard
from .mean_operations import MeanReversionOperations


CROSS_BORDER_MEAN_PAGE = "均值回复监控"
CROSS_BORDER_EXECUTABLE_PAGE = "实盘套利模拟"


class CrossBorderPanelConsoleDashboard:
    """Own the two cross-border pages and stop hidden periodic callbacks."""

    def __init__(self, project_root: Union[Path, str]) -> None:
        self.project_root = Path(project_root).resolve()
        self.mean_page: Optional[MeanReversionOperations] = None
        self.research_page: Optional[CrossBorderPanelDashboard] = None
        self.executable_page: Optional[PanelExecutableDashboard] = None
        initial_page = self._initial_page()
        self.page_select = pn.widgets.RadioButtonGroup(
            label="页面",
            options=[CROSS_BORDER_MEAN_PAGE, CROSS_BORDER_EXECUTABLE_PAGE],
            value=initial_page,
            color="primary",
            sizing_mode="stretch_width",
        )
        self.sidebar = pn.Column(sizing_mode="stretch_width")
        self.main = pn.Column(sizing_mode="stretch_width")
        self.page_select.param.watch(self._page_changed, "value")
        self._show_page(initial_page)

    def template(self):
        return pn.template.BootstrapTemplate(
            title="港股通跨境ETF研究控制台",
            site="ETF Arbitrage",
            header_background="#202A2E",
            sidebar=[self.sidebar],
            main=[self.main],
            sidebar_width=320,
        )

    def _page_changed(self, event) -> None:
        if (
            event.old == CROSS_BORDER_MEAN_PAGE
            and self.mean_page is not None
        ):
            self.mean_page.stop_runtime()
        elif (
            event.old == CROSS_BORDER_EXECUTABLE_PAGE
            and self.executable_page is not None
        ):
            self.executable_page.stop_runtime()
        self._show_page(event.new)

    def _show_page(self, page: str) -> None:
        if page == CROSS_BORDER_MEAN_PAGE:
            self._ensure_mean_page()
            assert self.mean_page is not None
            assert self.research_page is not None
            self.sidebar.objects = [
                self.page_select,
                pn.layout.Divider(),
                self.research_page.sidebar_controls(),
            ]
            self.main.objects = [
                pn.Tabs(
                    ("最新价监控", self.mean_page.view()),
                    ("官方PCF", self.research_page.pcf_view()),
                    ("采集接口", self.research_page.market_interface_view()),
                    ("汇率回填", self.research_page.research_view()),
                    ("审计", self.research_page.audit_view()),
                    dynamic=True,
                    sizing_mode="stretch_width",
                )
            ]
        else:
            self._ensure_executable_page()
            assert self.executable_page is not None
            self.sidebar.objects = [
                self.page_select,
                pn.layout.Divider(),
                self.executable_page.sidebar_controls(),
            ]
            self.main.objects = [self.executable_page.view()]

    def _ensure_mean_page(self) -> None:
        if self.mean_page is not None:
            return
        self.research_page = CrossBorderPanelDashboard(self.project_root)
        self.mean_page = MeanReversionOperations(
            self.project_root,
            pcf_controls=self.research_page.pcf_controls,
            default_etfs=("159920",),
            capture_only=True,
        )

    def _ensure_executable_page(self) -> None:
        if self.executable_page is None:
            self.executable_page = PanelExecutableDashboard(
                self.project_root,
                default_etf="159920",
                cross_border=True,
            )

    @staticmethod
    def _initial_page() -> str:
        values = (pn.state.session_args or {}).get("page", [])
        if not values:
            return CROSS_BORDER_MEAN_PAGE
        value = values[0] if isinstance(values, (list, tuple)) else values
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        normalized = str(value).strip().lower()
        if normalized in {
            "executable",
            "paper",
            "实盘套利模拟",
        }:
            return CROSS_BORDER_EXECUTABLE_PAGE
        return CROSS_BORDER_MEAN_PAGE


def build_cross_border_console_dashboard(
    project_root: Union[Path, str],
):
    return CrossBorderPanelConsoleDashboard(project_root).template()
