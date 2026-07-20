"""Operational dashboard for synthetic or replayed ETF arbitrage research."""

from dataclasses import asdict

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from etf_arbitrage.backtest import PremiumBacktester, ResearchReplay
from etf_arbitrage.config import BacktestConfig, MonitorConfig, RiskConfig
from etf_arbitrage.data import SyntheticDataFeed
from etf_arbitrage.monitor import PremiumMonitor
from etf_arbitrage.risk import RiskEngine
from etf_arbitrage.signal import FixedThresholdConfig, FixedThresholdSignal


ETF_OPTIONS = {
    "159919 沪深300ETF": "159919",
    "159915 创业板ETF": "159915",
    "159922 中证500ETF": "159922",
}


@st.cache_data(show_spinner=False)
def build_research(
    etf_code: str,
    periods: int,
    entry_threshold: float,
    max_suspension_ratio: float,
) -> tuple:
    feed = SyntheticDataFeed(etf_code=etf_code, periods=periods)
    monitor = PremiumMonitor(
        MonitorConfig(
            deviation_threshold=entry_threshold,
            recovery_threshold=min(0.001, entry_threshold / 2),
        )
    )
    risk_engine = RiskEngine(
        RiskConfig(max_suspension_ratio=max_suspension_ratio)
    )
    signal_engine = FixedThresholdSignal(
        FixedThresholdConfig(
            premium_entry=entry_threshold,
            discount_entry=entry_threshold,
        )
    )
    observations = ResearchReplay(
        feed,
        monitor=monitor,
        risk_engine=risk_engine,
        signal_engine=signal_engine,
    ).run(etf_code)
    backtest = PremiumBacktester(
        BacktestConfig(
            entry_threshold=entry_threshold,
            exit_threshold=min(0.001, entry_threshold / 2),
        )
    ).run(observations)
    return feed.get_etf_info(etf_code), observations, backtest


def premium_figure(frame: pd.DataFrame, threshold: float) -> go.Figure:
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=frame["timestamp"],
            y=frame["premium"] * 100,
            name="中间价折溢价",
            line={"color": "#15616D", "width": 2},
        )
    )
    figure.add_trace(
        go.Scatter(
            x=frame["timestamp"],
            y=frame["premium_at_bid"] * 100,
            name="溢价可执行边界",
            line={"color": "#D1495B", "width": 1},
        )
    )
    figure.add_trace(
        go.Scatter(
            x=frame["timestamp"],
            y=-frame["discount_at_ask"] * 100,
            name="折价可执行边界",
            line={"color": "#3A6EA5", "width": 1},
        )
    )
    figure.add_hline(y=threshold * 100, line_dash="dash", line_color="#D1495B")
    figure.add_hline(y=-threshold * 100, line_dash="dash", line_color="#3A6EA5")
    figure.update_layout(
        height=390,
        margin={"l": 10, "r": 10, "t": 25, "b": 10},
        yaxis_title="折溢价 (%)",
        xaxis_title=None,
        legend={"orientation": "h", "y": 1.12, "x": 0},
        hovermode="x unified",
    )
    return figure


def price_figure(frame: pd.DataFrame) -> go.Figure:
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=frame["timestamp"],
            y=frame["etf_price"],
            name="ETF 市价",
            line={"color": "#1B998B", "width": 2},
        )
    )
    figure.add_trace(
        go.Scatter(
            x=frame["timestamp"],
            y=frame["iopv"],
            name="IOPV",
            line={"color": "#2D3047", "width": 2},
        )
    )
    figure.update_layout(
        height=350,
        margin={"l": 10, "r": 10, "t": 25, "b": 10},
        yaxis_title="价格",
        xaxis_title=None,
        legend={"orientation": "h", "y": 1.12, "x": 0},
        hovermode="x unified",
    )
    return figure


def risk_figure(frame: pd.DataFrame) -> go.Figure:
    figure = go.Figure()
    colors = {
        "suspension_ratio": "#D1495B",
        "limit_up_ratio": "#F79256",
        "limit_down_ratio": "#3A6EA5",
        "low_liquidity_ratio": "#7A5195",
    }
    labels = {
        "suspension_ratio": "停牌权重",
        "limit_up_ratio": "涨停权重",
        "limit_down_ratio": "跌停权重",
        "low_liquidity_ratio": "低流动性权重",
    }
    for column, color in colors.items():
        figure.add_trace(
            go.Scatter(
                x=frame["timestamp"],
                y=frame[column] * 100,
                name=labels[column],
                line={"color": color, "width": 2},
            )
        )
    figure.update_layout(
        height=350,
        margin={"l": 10, "r": 10, "t": 25, "b": 10},
        yaxis_title="组合权重 (%)",
        xaxis_title=None,
        legend={"orientation": "h", "y": 1.12, "x": 0},
        hovermode="x unified",
    )
    return figure


def render() -> None:
    st.set_page_config(page_title="ETF Arbitrage v1.0", layout="wide")
    st.title("深市 ETF 单市场套利研究")
    st.caption("ETF Arbitrage v1.0 · 模拟分钟行情回放")

    with st.sidebar:
        st.subheader("研究参数")
        label = st.selectbox("ETF", list(ETF_OPTIONS))
        periods = st.slider("回放分钟数", 60, 240, 180, 30)
        threshold_pct = st.slider("机会阈值 (%)", 0.10, 1.00, 0.50, 0.05)
        suspension_pct = st.slider("最大停牌权重 (%)", 0.0, 30.0, 5.0, 1.0)
        st.divider()
        st.caption("数据源：内置确定性模拟行情")

    etf_code = ETF_OPTIONS[label]
    entry_threshold = threshold_pct / 100.0
    info, observations, backtest = build_research(
        etf_code, periods, entry_threshold, suspension_pct / 100.0
    )
    latest = observations.iloc[-1]
    opportunity_count = int(
        ((observations["signal"] != "none") & observations["signal_allowed"]).sum()
    )

    st.subheader("{} · {} · {}".format(info.name, info.etf_code, info.tracking_index))
    metrics = st.columns(6)
    metrics[0].metric("ETF 价格", "{:.4f}".format(latest.etf_price))
    metrics[1].metric("IOPV", "{:.4f}".format(latest.iopv))
    metrics[2].metric("当前折溢价", "{:.3%}".format(latest.premium))
    metrics[3].metric("最大绝对偏离", "{:.3%}".format(observations.premium.abs().max()))
    metrics[4].metric("有效机会", str(opportunity_count))
    metrics[5].metric("当前风险", "{} / {:.0f}".format(latest.risk_level.upper(), latest.risk_score))

    market_tab, risk_tab, backtest_tab, data_tab = st.tabs(
        ["偏离监控", "风险诊断", "价差回测", "研究数据"]
    )
    with market_tab:
        st.plotly_chart(
            premium_figure(observations, entry_threshold), use_container_width=True
        )
        st.plotly_chart(price_figure(observations), use_container_width=True)
        opportunities = observations[observations["signal"] != "none"].copy()
        st.subheader("机会记录")
        if opportunities.empty:
            st.info("当前阈值下没有识别到机会")
        else:
            opportunities["premium"] = opportunities["premium"].map(
                lambda value: "{:.3%}".format(value)
            )
            st.dataframe(
                opportunities[
                    [
                        "timestamp",
                        "signal",
                        "premium",
                        "signal_allowed",
                        "signal_reason",
                        "deviation_duration_seconds",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )

    with risk_tab:
        st.plotly_chart(risk_figure(observations), use_container_width=True)
        blocked = observations[observations["risk_blocked"]]
        st.subheader("风险阻断记录")
        if blocked.empty:
            st.success("回放区间内未触发风险阻断")
        else:
            st.dataframe(
                blocked[
                    [
                        "timestamp",
                        "risk_level",
                        "risk_score",
                        "suspension_ratio",
                        "etf_spread_bps",
                        "risk_blockers",
                    ]
                ],
                use_container_width=True,
                hide_index=True,
            )

    with backtest_tab:
        performance = backtest.performance
        columns = st.columns(5)
        columns[0].metric("价差收益", "{:.3%}".format(performance.total_return))
        columns[1].metric("Sharpe", "{:.2f}".format(performance.sharpe))
        columns[2].metric("最大回撤", "{:.3%}".format(performance.max_drawdown))
        columns[3].metric("胜率", "{:.1%}".format(backtest.win_rate))
        columns[4].metric("平均持有", "{:.1f} 分钟".format(backtest.average_holding_periods))
        equity_figure = go.Figure(
            go.Scatter(
                x=backtest.timeline["timestamp"],
                y=backtest.timeline["equity"],
                line={"color": "#15616D", "width": 2},
                name="净值",
            )
        )
        equity_figure.update_layout(
            height=350,
            margin={"l": 10, "r": 10, "t": 25, "b": 10},
            yaxis_title="模拟净值",
            xaxis_title=None,
        )
        st.plotly_chart(equity_figure, use_container_width=True)
        trades = pd.DataFrame([asdict(trade) for trade in backtest.trades])
        st.subheader("已平仓交易")
        if trades.empty:
            st.info("没有已完成的价差交易")
        else:
            st.dataframe(trades, use_container_width=True, hide_index=True)

    with data_tab:
        st.dataframe(observations, use_container_width=True, hide_index=True)
        st.download_button(
            "下载回放结果 CSV",
            observations.to_csv(index=False).encode("utf-8-sig"),
            file_name="{}_arbitrage_replay.csv".format(etf_code),
            mime="text/csv",
        )


if __name__ == "__main__":
    render()
