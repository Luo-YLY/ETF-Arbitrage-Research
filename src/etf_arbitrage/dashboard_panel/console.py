"""Two-page Panel application shell for ETF research workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

import panel as pn

from .app import PanelReplayDashboard
from .executable import PanelExecutableDashboard
from .mean_operations import MeanReversionOperations


MEAN_PAGE = "实盘均值回复监控"
EXECUTABLE_PAGE = "实盘套利模拟"


class PanelConsoleDashboard:
    """Own both pages and clear hidden runtime callbacks on navigation."""

    def __init__(self, project_root: Union[Path, str]) -> None:
        self.project_root = Path(project_root).resolve()
        self.mean_page: Optional[PanelReplayDashboard] = None
        self.mean_operations: Optional[MeanReversionOperations] = None
        self.executable_page: Optional[PanelExecutableDashboard] = None
        self._syncing_mean_selection = False
        initial_page = self._initial_page()
        self.page_select = pn.widgets.RadioButtonGroup(
            label="页面",
            options=[MEAN_PAGE, EXECUTABLE_PAGE],
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
            title="深市ETF套利研究控制台",
            site="ETF Arbitrage",
            header_background="#202A2E",
            sidebar=[self.sidebar],
            main=[self.main],
            sidebar_width=320,
        )

    def _page_changed(self, event) -> None:
        if event.old == MEAN_PAGE and self.mean_page is not None:
            self.mean_page.stop_runtime()
            if self.mean_operations is not None:
                self.mean_operations.stop_runtime()
        elif event.old == EXECUTABLE_PAGE and self.executable_page is not None:
            self.executable_page.stop_runtime()
        self._show_page(event.new)
        if event.new == MEAN_PAGE and self.mean_operations is not None:
            self.mean_operations.start_runtime()

    def _show_page(self, page: str) -> None:
        if page == MEAN_PAGE:
            self._ensure_mean_page()
            self.sidebar.objects = [
                self.page_select,
                pn.layout.Divider(),
                self.mean_page.sidebar_controls(),
            ]
            self.main.objects = [
                pn.Tabs(
                    ("盘前与采集", self.mean_operations.view()),
                    ("实时观察", self.mean_page.replay_view()),
                    ("收盘回测", self.mean_page.backtest_view()),
                    ("数据状态", self.mean_page.quality_view()),
                    dynamic=True,
                    sizing_mode="stretch_width",
                )
            ]
        else:
            self._ensure_executable_page()
            self.sidebar.objects = [
                self.page_select,
                pn.layout.Divider(),
                self.executable_page.sidebar_controls(),
            ]
            self.main.objects = [self.executable_page.view()]

    def _ensure_mean_page(self) -> None:
        if self.mean_page is not None:
            return
        self.mean_page = PanelReplayDashboard(self.project_root)
        self.mean_operations = MeanReversionOperations(
            self.project_root,
            on_observations_changed=self.mean_page.refresh_datasets,
        )
        self.mean_page.day_select.param.watch(
            self._sync_research_selection_to_pcf,
            "value",
        )
        self.mean_page.etf_select.param.watch(
            self._sync_research_selection_to_pcf,
            "value",
        )
        self.mean_operations.pcf.date_picker.param.watch(
            self._sync_pcf_selection_to_research,
            "value",
        )
        self.mean_operations.pcf.etf_select.param.watch(
            self._sync_pcf_selection_to_research,
            "value",
        )

    def _ensure_executable_page(self) -> None:
        if self.executable_page is None:
            self.executable_page = PanelExecutableDashboard(self.project_root)

    def _sync_research_selection_to_pcf(self, _event) -> None:
        if self._syncing_mean_selection or self.mean_operations is None:
            return
        self._syncing_mean_selection = True
        try:
            self.mean_operations.pcf.date_picker.value = (
                self.mean_page.day_select.value
            )
            self.mean_operations.pcf.etf_select.value = (
                self.mean_page.etf_select.value
            )
        finally:
            self._syncing_mean_selection = False

    def _sync_pcf_selection_to_research(self, _event) -> None:
        if self._syncing_mean_selection or self.mean_page is None:
            return
        self._syncing_mean_selection = True
        try:
            self.mean_page.day_select.value = (
                self.mean_operations.pcf.date_picker.value
            )
            self.mean_page.etf_select.value = (
                self.mean_operations.pcf.etf_select.value
            )
        finally:
            self._syncing_mean_selection = False

    @staticmethod
    def _initial_page() -> str:
        values = (pn.state.session_args or {}).get("page", [])
        if not values:
            return MEAN_PAGE
        value = values[0] if isinstance(values, (list, tuple)) else values
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="ignore")
        normalized = str(value).strip().lower()
        if normalized in {"executable", "paper", "实盘套利模拟"}:
            return EXECUTABLE_PAGE
        return MEAN_PAGE
