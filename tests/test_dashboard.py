import pandas as pd

from etf_arbitrage.dashboard.app import CHART_INTERVALS, _backtest_figure


def test_backtest_figure_shares_time_axis_and_hover_guide():
    timestamps = pd.date_range("2026-07-22 09:30:00", periods=6, freq="30s")
    observations = pd.DataFrame(
        {
            "timestamp": timestamps,
            "premium": [0.0010, 0.0025, 0.0018, 0.0004, -0.0022, -0.0003],
        }
    )
    timeline = pd.DataFrame(
        {
            "timestamp": timestamps,
            "equity": [1.0, 0.9997, 1.0002, 1.0010, 1.0007, 1.0015],
            "action": [
                "",
                "open_premium",
                "",
                "close_converged",
                "open_discount",
                "close_converged",
            ],
        }
    )

    figure = _backtest_figure(observations, timeline, 0.002, "1min")

    assert figure.layout.xaxis.matches == "x2"
    assert figure.layout.xaxis.showspikes is True
    assert figure.layout.xaxis2.visible is False
    assert {trace.xaxis for trace in figure.data} == {"x"}
    assert figure.layout.hovermode == "x unified"
    assert figure.layout.hoversubplots == "axis"
    assert len(figure.layout.shapes) == 2


def test_chart_intervals_include_raw_snapshots():
    assert list(CHART_INTERVALS) == ["1分钟", "30秒", "5分钟", "原始快照"]
    assert CHART_INTERVALS["原始快照"] is None
