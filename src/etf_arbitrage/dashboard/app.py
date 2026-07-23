"""Streamlit control center for the daily SZSE ETF research workflow."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from etf_arbitrage.backtest import PremiumBacktester, ResearchReplay
from etf_arbitrage.config import BacktestConfig
from etf_arbitrage.data import (
    DataFrameReplayFeed,
    JsonlSnapshotStore,
    PCFRepository,
    SZSE_ETFS,
    SZSEPCFParser,
    load_pcf_source_templates,
)
from etf_arbitrage.operations import (
    MarketMonitorController,
    MarketMonitorJob,
    SZSEMarketSchedule,
    read_job_state,
)
from etf_arbitrage.valuation import PCFIOPVCalculator
from etf_arbitrage.dashboard.page_state import (
    MEAN_REVERSION_PAGE,
    activate_dashboard_page,
)


ROOT = Path(__file__).resolve().parents[3]
PCF_ROOT = ROOT / "data" / "pcf"
SOURCE_CONFIG = ROOT / "config" / "pcf_sources.local.json"
ETF_LABELS = {
    "{} {}".format(profile.etf_code, profile.name): code
    for code, profile in SZSE_ETFS.items()
}
CHART_INTERVALS = {
    "1分钟": "1min",
    "30秒": "30s",
    "5分钟": "5min",
    "原始快照": None,
}


def _source_templates() -> Dict[str, str]:
    values = load_pcf_source_templates(ROOT / "config" / "pcf_sources.example.json")
    if SOURCE_CONFIG.exists():
        local = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
        values.update({str(code): str(url) for code, url in local.items() if url})
    for code in SZSE_ETFS:
        environment_value = os.getenv("ETF_PCF_URL_{}".format(code), "").strip()
        if environment_value:
            values[code] = environment_value
    return values


def _save_source_template(etf_code: str, url_template: str) -> None:
    payload: Dict[str, str] = {}
    if SOURCE_CONFIG.exists():
        payload = json.loads(SOURCE_CONFIG.read_text(encoding="utf-8"))
    payload[etf_code] = url_template.strip()
    SOURCE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    temporary = SOURCE_CONFIG.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(SOURCE_CONFIG)


def _read_jsonl(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    frame = pd.DataFrame(rows)
    if "timestamp" in frame:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    return frame


def _tail(path: Path, lines: int = 30) -> str:
    if not path.exists():
        return ""
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return "".join(handle.readlines()[-lines:])


def _find_pcf(
    repository: PCFRepository, etf_code: str, trading_day: str
) -> tuple[Optional[Path], Optional[str]]:
    try:
        return repository.find(etf_code, trading_day), None
    except Exception as exc:
        return None, str(exc)


def _resample_last(
    frame: pd.DataFrame,
    rule: Optional[str],
    required_column: str,
) -> pd.DataFrame:
    ordered = frame.copy()
    ordered["timestamp"] = pd.to_datetime(ordered["timestamp"])
    ordered.sort_values("timestamp", inplace=True)
    if rule is None or ordered.empty:
        return ordered
    return (
        ordered.set_index("timestamp")
        .resample(rule)
        .last()
        .dropna(subset=[required_column])
        .reset_index()
    )


def _premium_figure(
    frame: pd.DataFrame,
    entry_threshold: float,
    chart_rule: Optional[str],
) -> go.Figure:
    display = _resample_last(frame, chart_rule, "premium")
    figure = go.Figure()
    if chart_rule is not None and not frame.empty:
        source = frame.copy()
        source["timestamp"] = pd.to_datetime(source["timestamp"])
        band = (
            source.set_index("timestamp")["premium"]
            .resample(chart_rule)
            .agg(["min", "max"])
            .dropna()
            .reset_index()
        )
        figure.add_trace(
            go.Scatter(
                x=band["timestamp"],
                y=band["max"] * 100,
                name="区间上沿",
                line={"width": 0},
                hoverinfo="skip",
                showlegend=False,
            )
        )
        figure.add_trace(
            go.Scatter(
                x=band["timestamp"],
                y=band["min"] * 100,
                name="区间内范围",
                line={"width": 0},
                fill="tonexty",
                fillcolor="rgba(21, 97, 109, 0.16)",
                hovertemplate="区间下沿 %{y:.3f}%<extra></extra>",
            )
        )
    figure.add_trace(
        go.Scatter(
            x=display["timestamp"],
            y=display["premium"] * 100,
            name="区间末折溢价" if chart_rule else "最新价折溢价",
            line={"color": "#15616D", "width": 2},
        )
    )
    if "premium_at_bid" in display and display["premium_at_bid"].notna().any():
        figure.add_trace(
            go.Scatter(
                x=display["timestamp"],
                y=display["premium_at_bid"] * 100,
                name="买一溢价边界",
                line={"color": "#C44536", "width": 1},
            )
        )
    if "discount_at_ask" in display and display["discount_at_ask"].notna().any():
        figure.add_trace(
            go.Scatter(
                x=display["timestamp"],
                y=-display["discount_at_ask"] * 100,
                name="卖一折价边界",
                line={"color": "#3A6EA5", "width": 1},
            )
        )
    figure.add_hline(y=entry_threshold * 100, line_dash="dash", line_color="#C44536")
    figure.add_hline(y=-entry_threshold * 100, line_dash="dash", line_color="#3A6EA5")
    figure.update_layout(
        height=370,
        margin={"l": 8, "r": 8, "t": 25, "b": 8},
        yaxis_title="折溢价 (%)",
        xaxis_title=None,
        legend={"orientation": "h", "y": 1.14, "x": 0},
        hovermode="x unified",
    )
    return figure


def _backtest_figure(
    observations: pd.DataFrame,
    timeline: pd.DataFrame,
    entry_threshold: float,
    chart_rule: Optional[str],
) -> go.Figure:
    premium = _premium_figure(observations, entry_threshold, chart_rule)
    equity_display = _resample_last(timeline, chart_rule, "equity")
    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.08,
        row_heights=[0.55, 0.45],
        subplot_titles=("折溢价", "累计价差收益指数"),
    )
    for trace in premium.data:
        figure.add_trace(trace, row=1, col=1)
    for shape in premium.layout.shapes or ():
        figure.add_shape(shape, row=1, col=1)

    figure.add_trace(
        go.Scatter(
            x=equity_display["timestamp"],
            y=equity_display["equity"],
            name="Premium策略指数",
            line={"color": "#15616D", "width": 2},
        ),
        row=2,
        col=1,
    )
    trade_points = timeline[
        timeline["action"].str.startswith(("open_", "close_"))
    ]
    if not trade_points.empty:
        figure.add_trace(
            go.Scatter(
                x=trade_points["timestamp"],
                y=trade_points["equity"],
                name="开平仓",
                mode="markers",
                marker={"color": "#C44536", "size": 7},
                text=trade_points["action"],
                hovertemplate="%{text}<br>%{x}<br>%{y:.6f}<extra></extra>",
            ),
            row=2,
            col=1,
        )

    figure.update_yaxes(
        title_text="折溢价 (%)", tickformat=".3f", row=1, col=1
    )
    figure.update_yaxes(
        title_text="累计价差收益指数", tickformat=".4f", row=2, col=1
    )
    figure.update_traces(xaxis="x")
    figure.update_xaxes(
        showspikes=True,
        spikecolor="#6B7280",
        spikethickness=1,
        spikedash="dot",
        spikemode="across",
        spikesnap="cursor",
        row=1,
        col=1,
    )
    figure.update_layout(
        height=700,
        margin={"l": 76, "r": 20, "t": 65, "b": 52},
        legend={"orientation": "h", "y": 1.08, "x": 0},
        hovermode="x unified",
        hoversubplots="axis",
        hoverdistance=30,
        spikedistance=-1,
        xaxis={
            "anchor": "free",
            "position": 0,
            "showticklabels": True,
            "title_text": "时间",
        },
        xaxis2={"visible": False, "showticklabels": False},
    )
    return figure


def _price_figure(frame: pd.DataFrame, chart_rule: Optional[str]) -> go.Figure:
    display = _resample_last(frame, chart_rule, "iopv")
    figure = go.Figure()
    figure.add_trace(
        go.Scatter(
            x=display["timestamp"],
            y=display["etf_price"],
            name="ETF最新价",
            line={"color": "#1B998B", "width": 2},
        )
    )
    figure.add_trace(
        go.Scatter(
            x=display["timestamp"],
            y=display["iopv"],
            name="IOPV",
            line={"color": "#2D3047", "width": 2},
        )
    )
    figure.update_layout(
        height=330,
        margin={"l": 8, "r": 8, "t": 25, "b": 8},
        yaxis_title="价格",
        xaxis_title=None,
        legend={"orientation": "h", "y": 1.14, "x": 0},
        hovermode="x unified",
    )
    return figure


def _run_replay(
    etf_code: str,
    trading_day: str,
    entry_threshold: float,
    exit_threshold: float,
    max_holding_periods: int,
    transaction_cost_bps: float,
    mode: str,
) -> tuple[pd.DataFrame, Any]:
    pcf_path = PCFRepository(PCF_ROOT).find(etf_code, trading_day)
    if pcf_path is None:
        raise ValueError("未找到{}的当日PCF".format(etf_code))
    recording = ROOT / "tmp" / "recordings" / trading_day / "{}.jsonl".format(
        etf_code
    )
    etf_quotes, stock_quotes = JsonlSnapshotStore(recording).to_frames(etf_code)
    if etf_quotes.empty or stock_quotes.empty:
        raise ValueError("当日快照不足，尚不能回测")

    pcf = SZSEPCFParser().parse(pcf_path)
    feed = DataFrameReplayFeed(
        pcf.to_etf_info(), pcf.component_weights(), etf_quotes, stock_quotes
    )
    observations = ResearchReplay(feed, calculator=PCFIOPVCalculator(pcf)).run(etf_code)
    backtest_input = observations.copy()
    if mode == "indicative":
        backtest_input["risk_blocked"] = backtest_input["indicative_risk_blocked"]
    result = PremiumBacktester(
        BacktestConfig(
            entry_threshold=entry_threshold,
            exit_threshold=exit_threshold,
            max_holding_periods=max_holding_periods,
            transaction_cost_bps=transaction_cost_bps,
            execution_mode=mode,
        )
    ).run(backtest_input)
    return observations, result


def render() -> None:
    st.set_page_config(page_title="深市ETF套利研究控制台", layout="wide")
    activate_dashboard_page(st.session_state, MEAN_REVERSION_PAGE)
    st.title("深市ETF套利研究控制台")
    st.caption("盘前PCF准备 · 交易时段行情采集 · 收盘后均值回复回测")

    repository = PCFRepository(PCF_ROOT)
    controller = MarketMonitorController(ROOT)
    schedule = SZSEMarketSchedule()

    with st.sidebar:
        st.subheader("运行参数")
        selected_labels = st.multiselect(
            "监控ETF",
            list(ETF_LABELS),
            default=list(ETF_LABELS)[:1],
        )
        selected_codes = [ETF_LABELS[label] for label in selected_labels]
        selected_day = st.date_input("交易日", value=date.today())
        trading_day = selected_day.strftime("%Y%m%d")
        interval = st.number_input("采集间隔（秒）", 1.0, 30.0, 3.0, 1.0)
        st.divider()
        st.subheader("均值回复参数")
        entry_pct = st.number_input("开仓阈值（%）", 0.01, 5.0, 0.20, 0.01)
        exit_pct = st.number_input("平仓阈值（%）", 0.0, 2.0, 0.05, 0.01)
        max_holding = st.number_input("最长持有期（快照数）", 1, 10000, 600, 10)
        cost_bps = st.number_input("单边换仓成本（bp）", 0.0, 100.0, 3.0, 0.5)
        mode_label = st.selectbox(
            "回测价格模式", ["指示性（最新价）", "可执行（买一卖一）"]
        )
        mode = "indicative" if mode_label.startswith("指示性") else "executable"
        st.divider()
        st.subheader("图表显示")
        chart_interval_label = st.radio("图表频率", list(CHART_INTERVALS))
        chart_rule = CHART_INTERVALS[chart_interval_label]

    entry_threshold = float(entry_pct) / 100.0
    exit_threshold = float(exit_pct) / 100.0
    pcf_results = {
        code: _find_pcf(repository, code, trading_day) for code in selected_codes
    }
    pcf_paths = {code: result[0] for code, result in pcf_results.items()}
    pcf_errors = {code: result[1] for code, result in pcf_results.items() if result[1]}
    state_path = controller.state_path(trading_day)
    state = read_job_state(state_path)

    control_tab, live_tab, backtest_tab = st.tabs(
        ["盘前与采集", "实时观察", "收盘回测"]
    )

    with control_tab:
        st.subheader("1. PCF准备")
        if not selected_codes:
            st.info("请先在左侧选择至少一只ETF。")
        else:
            rows = []
            for code in selected_codes:
                path = pcf_paths[code]
                profile = SZSE_ETFS[code]
                rows.append(
                    {
                        "ETF": code,
                        "名称": profile.name,
                        "基金公司": profile.manager,
                        "PCF状态": (
                            "校验失败" if code in pcf_errors else "已校验" if path else "缺失"
                        ),
                        "本地文件": str(path.relative_to(ROOT)) if path else "",
                    }
                )
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            for code, error in pcf_errors.items():
                st.error("{} PCF校验失败：{}".format(code, error))

            target_code = st.selectbox("准备哪只ETF的PCF", selected_codes)
            sources = _source_templates()
            url_key = "pcf_url_{}".format(target_code)
            if url_key not in st.session_state:
                st.session_state[url_key] = sources.get(target_code, "")
            url_template = st.text_input(
                "PCF来源地址（已自动配置，一般无需修改）",
                key=url_key,
                placeholder="支持深交所下载页面、XML直链及日期/代码占位符",
            )
            download_col, save_col = st.columns([1, 1])
            if download_col.button("下载并校验PCF", use_container_width=True):
                try:
                    path = repository.download(url_template, target_code, trading_day)
                    st.success("PCF已保存：{}".format(path.relative_to(ROOT)))
                    st.rerun()
                except Exception as exc:
                    st.error("下载失败：{}".format(exc))
            if save_col.button("保存本机下载地址", use_container_width=True):
                _save_source_template(target_code, url_template)
                st.success("下载地址已保存到本机配置。")

            upload = st.file_uploader(
                "或上传已经下载的PCF（XML或ZIP）",
                type=["xml", "zip"],
                key="pcf_upload_{}_{}".format(target_code, trading_day),
            )
            if upload is not None and st.button("导入并校验上传文件"):
                try:
                    path = repository.save(upload.getvalue(), target_code, trading_day)
                    st.success("PCF已导入：{}".format(path.relative_to(ROOT)))
                    st.rerun()
                except Exception as exc:
                    st.error("导入失败：{}".format(exc))

        st.subheader("2. 日内采集")
        status_cols = st.columns(4)
        status_cols[0].metric("当前时段", schedule.phase(pd.Timestamp.now().to_pydatetime()))
        status_cols[1].metric("Redis配置", "已配置" if os.getenv("SZ_REDIS_HOST") else "未配置")
        status_cols[2].metric("任务状态", state.get("status", "未启动"))
        status_cols[3].metric("累计轮询", str(state.get("polls", 0)))

        ready = bool(selected_codes) and all(pcf_paths.values()) and bool(
            os.getenv("SZ_REDIS_HOST")
        )
        start_col, stop_col, refresh_col = st.columns(3)
        if start_col.button("启动当日采集", disabled=not ready, use_container_width=True):
            try:
                pid = controller.start(
                    MarketMonitorJob(
                        trade_date=trading_day,
                        etf_codes=tuple(selected_codes),
                        pcf_paths={code: str(pcf_paths[code]) for code in selected_codes},
                        interval=float(interval),
                        redis_code_suffix=".SZ",
                    )
                )
                st.success("后台采集已启动，进程号 {}。".format(pid))
                st.rerun()
            except Exception as exc:
                st.error("启动失败：{}".format(exc))
        if stop_col.button("停止采集", use_container_width=True):
            controller.request_stop(trading_day)
            st.info("已发送停止请求，采集器会在当前轮结束后退出。")
        if refresh_col.button("刷新任务状态", use_container_width=True):
            st.rerun()

        if not ready:
            missing = [code for code, path in pcf_paths.items() if path is None]
            if missing:
                st.warning("缺少当日PCF：{}".format(", ".join(missing)))
            if not os.getenv("SZ_REDIS_HOST"):
                st.warning("当前进程未设置 SZ_REDIS_HOST，暂不能启动内网采集。")
        if state.get("latest"):
            latest_rows = [
                dict(ETF=code, **value) for code, value in state["latest"].items()
            ]
            st.dataframe(pd.DataFrame(latest_rows), use_container_width=True, hide_index=True)
        log_text = _tail(controller.log_path(trading_day))
        if log_text:
            with st.expander("采集日志"):
                st.code(log_text, language="text")

    with live_tab:
        st.subheader("当日实时观察")
        if not selected_codes:
            st.info("请先选择ETF。")
        else:
            live_code = st.selectbox("查看ETF", selected_codes, key="live_code")
            observation_path = (
                ROOT / "tmp" / "observations" / trading_day / "{}.jsonl".format(live_code)
            )
            frame = _read_jsonl(observation_path)
            if st.button("刷新实时数据"):
                st.rerun()
            if frame.empty:
                st.info("尚无派生观察记录；采集到有效行情后这里会显示价格、IOPV和Premium。")
            else:
                latest = frame.iloc[-1]
                metrics = st.columns(6)
                metrics[0].metric("ETF最新价", "{:.4f}".format(latest["etf_price"]))
                metrics[1].metric("IOPV", "{:.4f}".format(latest["iopv"]))
                metrics[2].metric("Premium", "{:.3%}".format(latest["premium"]))
                metrics[3].metric("最大绝对偏离", "{:.3%}".format(frame["premium"].abs().max()))
                metrics[4].metric("记录数", str(len(frame)))
                metrics[5].metric("估值质量", str(latest.get("valuation_quality", "")))
                st.plotly_chart(
                    _premium_figure(frame, entry_threshold, chart_rule),
                    use_container_width=True,
                )
                st.plotly_chart(
                    _price_figure(frame, chart_rule), use_container_width=True
                )
                display_columns = [
                    column
                    for column in [
                        "timestamp",
                        "etf_price",
                        "iopv",
                        "premium",
                        "signal",
                        "signal_allowed",
                        "valuation_quality",
                        "risk_blockers",
                    ]
                    if column in frame
                ]
                st.dataframe(
                    frame[display_columns].tail(200),
                    use_container_width=True,
                    hide_index=True,
                )

    with backtest_tab:
        st.subheader("收盘后均值回复回测")
        if not selected_codes:
            st.info("请先选择ETF。")
        else:
            replay_code = st.selectbox("回测ETF", selected_codes, key="replay_code")
            recording_path = (
                ROOT / "tmp" / "recordings" / trading_day / "{}.jsonl".format(replay_code)
            )
            pcf_path = pcf_paths.get(replay_code)
            checks = st.columns(3)
            checks[0].metric("行情记录", "已找到" if recording_path.exists() else "缺失")
            checks[1].metric("当日PCF", "已校验" if pcf_path else "缺失")
            checks[2].metric("价格模式", mode_label)
            if mode == "indicative":
                st.info("当前Redis没有买一卖一时，回测仅衡量折溢价收敛，不代表可直接成交收益。")

            run_key = "replay_result_{}_{}".format(replay_code, trading_day)
            if st.button(
                "运行收盘回测",
                disabled=not recording_path.exists() or pcf_path is None,
                use_container_width=True,
            ):
                try:
                    st.session_state[run_key] = _run_replay(
                        replay_code,
                        trading_day,
                        entry_threshold,
                        exit_threshold,
                        int(max_holding),
                        float(cost_bps),
                        mode,
                    )
                except Exception as exc:
                    st.error("回测失败：{}".format(exc))

            if run_key in st.session_state:
                observations, result = st.session_state[run_key]
                performance = result.performance
                metrics = st.columns(5)
                metrics[0].metric("价差收益", "{:.3%}".format(performance.total_return))
                metrics[1].metric("Sharpe", "{:.2f}".format(performance.sharpe))
                metrics[2].metric("最大回撤", "{:.3%}".format(performance.max_drawdown))
                metrics[3].metric("胜率", "{:.1%}".format(result.win_rate))
                metrics[4].metric("平均持有", "{:.1f}个快照".format(result.average_holding_periods))
                st.plotly_chart(
                    _backtest_figure(
                        observations,
                        result.timeline,
                        entry_threshold,
                        chart_rule,
                    ),
                    use_container_width=True,
                )
                trades = pd.DataFrame([asdict(trade) for trade in result.trades])
                if trades.empty:
                    st.info("当前参数下没有完成的交易。")
                else:
                    st.dataframe(trades, use_container_width=True, hide_index=True)
                st.download_button(
                    "下载观察结果CSV",
                    observations.to_csv(index=False).encode("utf-8-sig"),
                    file_name="{}_{}_observations.csv".format(replay_code, trading_day),
                    mime="text/csv",
                )


if __name__ == "__main__":
    render()
