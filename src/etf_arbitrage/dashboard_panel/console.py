"""Two-page Panel application shell for ETF research workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Union

import panel as pn

from .app import ACCENT, RAW_CSS, PanelReplayDashboard
from .executable import EXECUTABLE_CSS, PanelExecutableDashboard
from .mean_operations import MeanReversionOperations


MEAN_PAGE = "实盘均值回复监控"
EXECUTABLE_PAGE = "实盘套利模拟"


class PanelConsoleDashboard:
    """Own both pages and clear hidden runtime callbacks on navigation."""

    def __init__(self, project_root: Union[Path, str]) -> None:
        self.project_root = Path(project_root).resolve()
        self.mean_page = PanelReplayDashboard(self.project_root)
        self.mean_operations = MeanReversionOperations(
            self.project_root,
            on_observations_changed=self.mean_page.refresh_datasets,
        )
        self.executable_page = PanelExecutableDashboard(self.project_root)
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
        return pn.template.FastListTemplate(
            title="深市ETF套利研究控制台",
            site="ETF Arbitrage",
            accent_base_color=ACCENT,
            header_background="#202A2E",
            sidebar=[self.sidebar],
            main=[self.main],
            sidebar_width=320,
            main_layout=None,
            raw_css=[RAW_CSS, EXECUTABLE_CSS],
        )

    def _page_changed(self, event) -> None:
        if event.old == MEAN_PAGE:
            self.mean_page.stop_runtime()
            self.mean_operations.stop_runtime()
        elif event.old == EXECUTABLE_PAGE:
            self.executable_page.stop_runtime()
        self._show_page(event.new)

    def _show_page(self, page: str) -> None:
        if page == MEAN_PAGE:
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
                    dynamic=False,
                    sizing_mode="stretch_width",
                )
            ]
        else:
            self.sidebar.objects = [
                self.page_select,
                pn.layout.Divider(),
                self.executable_page.sidebar_controls(),
            ]
            self.main.objects = [self.executable_page.view()]

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
