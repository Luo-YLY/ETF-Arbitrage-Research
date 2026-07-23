"""Streamlit control surface for the paper-only executable arbitrage engine."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from io import BytesIO
import json
from pathlib import Path
import time
from typing import Any, Dict, Optional

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from etf_arbitrage.data import (
    PCFRepository,
    SZSE_ETFS,
    load_pcf_source_templates,
    validate_executable_pcf,
)
from etf_arbitrage.engine import PaperArbitrageEngine, PaperEngineResult
from etf_arbitrage.executable_config import (
    AccountConfig,
    CostConfig,
    DataQualityConfig,
    DataSourceMode,
    DirectionSelection,
    ExecutionConfig,
    ExecutionMode,
    ExecutionScenario,
    FileReplayConfig,
    PaperArbitrageConfig,
    PrimaryMarketConfig,
    RedisConfig,
    SimulationConfig,
    SimulationScenario,
)
from etf_arbitrage.market_data import (
    DynamicMarketDataLoader,
    FileReplayMarketDataSource,
    MarketDataSource,
    RedisMarketDataSource,
    SimulatedMarketDataSource,
)
from etf_arbitrage.reporting import RunRecorder


ROOT = Path(__file__).resolve().parents[3]
PCF_ROOT = ROOT / "data" / "pcf"
RUN_ROOT = ROOT / "data" / "runs"

SIMULATION_SCENARIO_LABELS = {
    SimulationScenario.NORMAL: "正常行情",
    SimulationScenario.PREMIUM_SHOCK: "ETF溢价冲击",
    SimulationScenario.DISCOUNT_SHOCK: "ETF折价冲击",
    SimulationScenario.MEAN_REVERSION: "折溢价均值回复",
    SimulationScenario.ETF_DEPTH_SHORTAGE: "ETF盘口深度不足",
    SimulationScenario.COMPONENT_DEPTH_SHORTAGE: "成分股盘口深度不足",
    SimulationScenario.STALE_QUOTE: "成分股报价陈旧",
    SimulationScenario.MISSING_QUOTE: "成分股行情缺失",
    SimulationScenario.SUSPENSION: "成分股停牌",
    SimulationScenario.LIMIT_UP_NO_ASK: "涨停且无卖盘",
    SimulationScenario.LIMIT_DOWN_NO_BID: "跌停且无买盘",
    SimulationScenario.DECODE_ERROR: "行情解码错误",
    SimulationScenario.SEQUENCE_GAP: "行情序号断档",
    SimulationScenario.CROSSED_BOOK: "异常交叉盘口",
    SimulationScenario.PCF_INVALID: "PCF交易日无效",
}

SYSTEM_STATUS_LABELS = {
    "RUNNING": "运行中",
    "READY": "就绪",
    "DISCONNECTED": "未连接",
    "DISABLED": "已禁用",
    "ERROR": "异常",
}


def _latest_local_day(etf_code: str) -> date:
    paths = sorted(PCF_ROOT.glob("*/pcf_{}_*.xml".format(etf_code)), reverse=True)
    if paths:
        try:
            return datetime.strptime(paths[0].parent.name, "%Y%m%d").date()
        except ValueError:
            pass
    return date.today()


def _runtime_signature(path: Path, config: PaperArbitrageConfig) -> str:
    return "{}:{}:{}".format(path, path.stat().st_mtime_ns, json.dumps(config.to_dict(), sort_keys=True))


def _new_history() -> Dict[str, list[dict]]:
    return {
        "snapshots": [],
        "opportunities": [],
        "rejected_opportunities": [],
        "orders": [],
        "fills": [],
        "primary_market_requests": [],
        "trades": [],
        "pnl": [],
        "data_quality_events": [],
    }


def _candlestick_frame(rows: pd.DataFrame, ticks_per_candle: int) -> pd.DataFrame:
    if rows.empty or "etf_last" not in rows:
        return pd.DataFrame()
    prices = rows.loc[:, ["timestamp", "etf_last"]].copy()
    prices["timestamp"] = pd.to_datetime(prices["timestamp"])
    prices["etf_last"] = pd.to_numeric(prices["etf_last"], errors="coerce")
    prices.dropna(subset=["etf_last"], inplace=True)
    if prices.empty:
        return pd.DataFrame()
    prices["candle"] = range(len(prices))
    prices["candle"] = prices["candle"] // max(1, ticks_per_candle)
    return (
        prices.groupby("candle", sort=True)
        .agg(
            timestamp=("timestamp", "first"),
            open=("etf_last", "first"),
            high=("etf_last", "max"),
            low=("etf_last", "min"),
            close=("etf_last", "last"),
        )
        .reset_index(drop=True)
    )


def _book_rows(book) -> pd.DataFrame:
    rows = []
    maximum = max(len(book.bids), len(book.asks), 1)
    for index in range(maximum):
        bid = book.bids[index] if index < len(book.bids) else None
        ask = book.asks[index] if index < len(book.asks) else None
        rows.append(
            {
                "档位": index + 1,
                "买量": bid.quantity if bid else None,
                "买价": bid.price if bid else None,
                "卖价": ask.price if ask else None,
                "卖量": ask.quantity if ask else None,
            }
        )
    return pd.DataFrame(rows)


def _component_book_rows(snapshot) -> pd.DataFrame:
    rows = []
    for symbol, book in snapshot.component_order_books.items():
        rows.append(
            {
                "证券代码": symbol,
                "状态": book.trading_status.value,
                "最新价": book.last_price,
                "买一": book.best_bid,
                "买一量": book.bids[0].quantity if book.bids else None,
                "卖一": book.best_ask,
                "卖一量": book.asks[0].quantity if book.asks else None,
                "行情时间": book.exchange_timestamp,
                "序号": book.sequence_number,
            }
        )
    return pd.DataFrame(rows)


def _snapshot_record(snapshot, evaluation) -> dict:
    component_payload = {
        symbol: {
            "timestamp": book.exchange_timestamp.isoformat(),
            "status": book.trading_status.value,
            "bids": [[level.price, level.quantity] for level in book.bids],
            "asks": [[level.price, level.quantity] for level in book.asks],
        }
        for symbol, book in snapshot.component_order_books.items()
    }
    return {
        "timestamp": snapshot.snapshot_timestamp.isoformat(),
        "source_mode": snapshot.source_mode,
        "etf_code": snapshot.etf_order_book.symbol,
        "etf_last": snapshot.etf_order_book.last_price,
        "etf_bid": snapshot.etf_order_book.best_bid,
        "etf_ask": snapshot.etf_order_book.best_ask,
        "official_iopv": snapshot.official_iopv,
        "internal_iopv": evaluation.internal_iopv,
        "sequence_gap": snapshot.sequence_gap,
        "decode_error": snapshot.decode_error,
        "component_books_json": json.dumps(component_payload, ensure_ascii=False),
    }


def _opportunity_record(evaluation, result) -> dict:
    return {
        "timestamp": evaluation.timestamp.isoformat(),
        "etf_code": evaluation.etf_code,
        "direction": result.direction.value,
        "cu_count": result.cu_count,
        "gross_profit": result.gross_profit,
        "estimated_costs": result.estimated_costs,
        "safety_buffer": result.safety_buffer,
        "net_profit": result.net_profit,
        "net_profit_bps": result.net_profit_bps,
        "executable": result.executable,
        "rejection_reasons": ",".join(result.rejection_reasons),
        "basket_value": result.basket.total_value,
        "basket_fully_filled": result.basket.fully_filled,
        "etf_fully_filled": result.etf_sweep.fully_filled,
    }


def _append_history(history: Dict[str, list[dict]], snapshot, result: PaperEngineResult) -> None:
    evaluation = result.decision_evaluation
    history["snapshots"].append(_snapshot_record(snapshot, evaluation))
    for direction in (evaluation.creation, evaluation.redemption):
        row = _opportunity_record(evaluation, direction)
        history["opportunities"].append(row)
        if not direction.executable:
            history["rejected_opportunities"].append(row)
    history["data_quality_events"].append(
        dict(timestamp=evaluation.timestamp.isoformat(), **evaluation.quality.to_dict())
    )
    if result.cycle is None:
        return
    cycle = result.cycle
    history["trades"].append(
        {
            "cycle_id": cycle.cycle_id,
            "direction": cycle.direction,
            "decision_time": cycle.decision_time.isoformat(),
            "fill_time": cycle.fill_time.isoformat() if cycle.fill_time else None,
            "state": cycle.state.value,
            "state_history": ",".join(cycle.state_history),
            "snapshot_profit": cycle.snapshot_profit,
            "execution_profit": cycle.execution_profit,
            "final_pnl": cycle.final_pnl,
            "rejection_reason": cycle.rejection_reason,
            "execution_risk_label": cycle.execution_risk_label,
        }
    )
    for order in cycle.orders:
        row = asdict(order)
        row["submit_time"] = order.submit_time.isoformat()
        history["orders"].append(row)
    for fill in cycle.fills:
        row = asdict(fill)
        row["fill_time"] = fill.fill_time.isoformat()
        history["fills"].append(row)
    history["pnl"].append(
        {
            "timestamp": (cycle.fill_time or cycle.decision_time).isoformat(),
            "cycle_id": cycle.cycle_id,
            "snapshot_pnl": cycle.snapshot_profit,
            "execution_pnl": cycle.execution_profit,
            "final_pnl": cycle.final_pnl,
        }
    )
    if result.primary_request is not None:
        request = result.primary_request
        row = asdict(request)
        row["status"] = request.status.value
        row["submit_time"] = request.submit_time.isoformat()
        row["confirm_time"] = request.confirm_time.isoformat() if request.confirm_time else None
        row["final_settlement_time"] = (
            request.final_settlement_time.isoformat() if request.final_settlement_time else None
        )
        row["pcf_date"] = request.pcf_date.isoformat()
        row["status_history"] = ",".join(request.status_history)
        history["primary_market_requests"].append(row)


def _advance(source: MarketDataSource, engine: PaperArbitrageEngine) -> PaperEngineResult:
    snapshot = source.step()
    fill_snapshot = None
    if engine.config.execution.auto_paper_trade and not engine.config.execution.record_opportunities_only:
        target = snapshot.snapshot_timestamp + timedelta(
            milliseconds=(
                engine.config.execution.decision_latency_ms
                + engine.config.execution.order_latency_ms
            )
        )
        fill_snapshot = source.snapshot_at_or_after(target)
    result = engine.process(snapshot, fill_snapshot)
    st.session_state.exec_snapshot = snapshot
    st.session_state.exec_result = result
    _append_history(st.session_state.exec_history, snapshot, result)
    return result


def _source_for(config: PaperArbitrageConfig, pcf) -> Optional[MarketDataSource]:
    if config.data_source == DataSourceMode.SIMULATED:
        return SimulatedMarketDataSource(pcf, config.simulation)
    if config.data_source == DataSourceMode.FILE_REPLAY:
        return st.session_state.get("exec_file_source")
    return RedisMarketDataSource(config.redis, config.etf_code)


def _table_download(table: str, rows: list[dict]) -> None:
    frame = pd.DataFrame(rows)
    left, right = st.columns(2)
    left.download_button(
        "下载 {} CSV".format(table),
        frame.to_csv(index=False).encode("utf-8-sig"),
        file_name="{}.csv".format(table),
        mime="text/csv",
        use_container_width=True,
    )
    buffer = BytesIO()
    try:
        frame.to_parquet(buffer, index=False)
        right.download_button(
            "下载 {} Parquet".format(table),
            buffer.getvalue(),
            file_name="{}.parquet".format(table),
            mime="application/octet-stream",
            use_container_width=True,
        )
    except ImportError:
        right.info("Parquet需要pyarrow。")


def render() -> None:
    st.set_page_config(page_title="深市ETF可执行套利模拟", layout="wide")
    st.title("深市ETF可执行申赎套利模拟")
    st.caption("纸面模拟系统 · 无真实下单接口")

    repository = PCFRepository(PCF_ROOT)
    labels = {"{} {}".format(code, profile.name): code for code, profile in SZSE_ETFS.items()}
    with st.sidebar:
        st.subheader("基础设置")
        selected_label = st.selectbox("ETF", list(labels))
        etf_code = labels[selected_label]
        trading_date = st.date_input("交易日", value=_latest_local_day(etf_code))
        source_mode = DataSourceMode(st.selectbox("数据源", [item.value for item in DataSourceMode]))

        st.subheader("PCF")
        pcf_mode = st.selectbox("PCF来源", ["LOCAL_FILE", "OFFICIAL_DOWNLOAD", "UPLOAD"])
        day_text = trading_date.strftime("%Y%m%d")
        pcf_path = repository.find(etf_code, day_text)
        if pcf_mode == "OFFICIAL_DOWNLOAD":
            templates = load_pcf_source_templates(ROOT / "config" / "pcf_sources.example.json")
            url = st.text_input("官方PCF地址", value=templates.get(etf_code, ""))
            if st.button("下载官方PCF", use_container_width=True):
                try:
                    repository.download(url, etf_code, day_text)
                    st.success("PCF下载并校验完成。")
                    st.rerun()
                except Exception as exc:
                    st.error("PCF下载失败：{}".format(exc))
        elif pcf_mode == "UPLOAD":
            upload = st.file_uploader("上传PCF XML或ZIP", type=["xml", "zip"])
            if upload is not None and st.button("载入上传PCF", use_container_width=True):
                try:
                    repository.save(upload.getvalue(), etf_code, day_text)
                    st.success("PCF载入并校验完成。")
                    st.rerun()
                except Exception as exc:
                    st.error("PCF载入失败：{}".format(exc))
        else:
            local_value = str(pcf_path) if pcf_path else ""
            local_path = st.text_input("本地PCF路径", value=local_value)
            if st.button("重新加载PCF", use_container_width=True):
                try:
                    candidate = Path(local_path)
                    _, report = validate_executable_pcf(candidate, etf_code, trading_date)
                    if not report.valid:
                        raise ValueError(", ".join(report.errors))
                    st.session_state.exec_custom_pcf = str(candidate)
                    st.rerun()
                except Exception as exc:
                    st.error("PCF校验失败：{}".format(exc))
            custom = st.session_state.get("exec_custom_pcf")
            if custom:
                candidate = Path(custom)
                try:
                    validate_executable_pcf(candidate, etf_code, trading_date)
                    pcf_path = candidate
                except Exception:
                    pass

        st.subheader("行情与回放")
        simulation_scenario = SimulationScenario.NORMAL
        random_seed = 42
        tick_ms = 1_000
        total_ticks = 300
        playback_speed = 1.0
        premium_shock = 35.0
        levels = 5
        depth = 2.0
        volatility = 0.00015
        etf_spread = 2.0
        component_spread = 4.0
        depth_decay = 0.85
        shock_start_tick = 0
        shock_duration_ticks = 30
        mean_reversion_speed = 0.12
        stale_ratio = 0.10
        missing_ratio = 0.05
        suspended_weight = 0.05
        limit_up_weight = 0.05
        limit_down_weight = 0.05
        sequence_gap_probability = 0.0
        quote_latency = 50
        if source_mode == DataSourceMode.SIMULATED:
            simulation_scenario = st.selectbox(
                "模拟场景",
                list(SimulationScenario),
                format_func=lambda scenario: SIMULATION_SCENARIO_LABELS[scenario],
            )
            random_seed = int(st.number_input("随机种子", 0, 1_000_000, 42))
            tick_ms = int(st.number_input("Tick间隔（毫秒）", 10, 60_000, 1_000, 10))
            total_ticks = int(st.number_input("模拟时间轴长度（Tick）", 10, 100_000, 300, 10))
            playback_speed = float(
                st.select_slider(
                    "模拟加速",
                    options=[0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0],
                    value=1.0,
                    format_func=lambda value: "{}x".format(value),
                )
            )
            premium_shock = float(st.number_input("冲击（bp）", 0.0, 500.0, 35.0, 1.0))
            volatility = float(st.number_input("基础波动率", 0.0, 0.02, 0.00015, 0.00005, format="%.5f"))
            levels = int(st.number_input("盘口档位", 1, 10, 5))
            depth = float(st.number_input("每档深度（CU倍数）", 0.01, 20.0, 2.0, 0.1))
            quote_latency = int(st.number_input("行情延迟（毫秒）", 0, 60_000, 50, 10))
            stale_ratio = float(st.slider("陈旧报价比例", 0.0, 1.0, 0.10, 0.01))
            missing_ratio = float(st.slider("缺失报价比例", 0.0, 1.0, 0.05, 0.01))
            with st.expander("模拟行情高级参数"):
                etf_spread = float(st.number_input("ETF价差（bp）", 0.0, 1_000.0, 2.0, 0.5))
                component_spread = float(st.number_input("成分股价差（bp）", 0.0, 1_000.0, 4.0, 0.5))
                depth_decay = float(st.slider("盘口深度衰减", 0.01, 1.0, 0.85, 0.01))
                shock_start_tick = int(st.number_input("冲击开始Tick", 0, 1_000_000, 0))
                shock_duration_ticks = int(st.number_input("冲击持续Tick", 1, 1_000_000, 30))
                mean_reversion_speed = float(st.number_input("均值回复速度", 0.0, 10.0, 0.12, 0.01))
                suspended_weight = float(st.slider("模拟停牌比例", 0.0, 1.0, 0.05, 0.01))
                limit_up_weight = float(st.slider("模拟涨停比例", 0.0, 1.0, 0.05, 0.01))
                limit_down_weight = float(st.slider("模拟跌停比例", 0.0, 1.0, 0.05, 0.01))
                sequence_gap_probability = float(st.slider("序号断档概率", 0.0, 1.0, 0.0, 0.01))
        elif source_mode == DataSourceMode.FILE_REPLAY:
            replay_upload = st.file_uploader("历史行情文件", type=["csv", "json", "jsonl", "parquet"])
            replay_path = st.text_input("本地文件/文件夹/通配符")
            playback_speed = float(st.number_input("回放速度（倍）", 0.1, 100.0, 1.0, 0.1))
            mapping_text = st.text_area("Schema映射JSON", value="{}", height=80)
            if st.button("加载或重新加载历史行情", use_container_width=True):
                try:
                    mapping = json.loads(mapping_text or "{}")
                    loader = DynamicMarketDataLoader()
                    if replay_upload is not None:
                        snapshots, summary = loader.load_bytes(
                            replay_upload.getvalue(), replay_upload.name, mapping
                        )
                    else:
                        snapshots, summary = loader.load_path(replay_path, mapping)
                    st.session_state.exec_file_source = FileReplayMarketDataSource(snapshots, summary)
                    st.session_state.exec_file_summary = summary
                    st.session_state.pop("exec_signature", None)
                    st.rerun()
                except Exception as exc:
                    st.error("历史行情加载失败：{}".format(exc))

        redis_enabled = False
        redis_host = "localhost"
        redis_port = 6379
        redis_db = 0
        redis_password = None
        redis_key_prefix = "etf_arbitrage"
        redis_channel_pattern = "market:*"
        redis_timeout = 2.0
        with st.expander("Redis高级设置"):
            redis_enabled = st.checkbox("启用Redis", value=False)
            redis_host = st.text_input("Host", value="localhost")
            redis_port = int(st.number_input("Port", 1, 65535, 6379))
            redis_db = int(st.number_input("DB", 0, 100, 0))
            redis_password = st.text_input("Password", type="password") or None
            redis_key_prefix = st.text_input("Key prefix", value="etf_arbitrage")
            redis_channel_pattern = st.text_input("Channel pattern", value="market:*")
            redis_timeout = float(st.number_input("连接超时（秒）", 0.1, 60.0, 2.0, 0.1))

        st.subheader("执行参数")
        direction = DirectionSelection(
            st.selectbox("套利方向", [item.value for item in DirectionSelection])
        )
        execution_mode = ExecutionMode(
            st.selectbox("执行模式", [item.value for item in ExecutionMode])
        )
        execution_scenario = ExecutionScenario(
            st.selectbox("执行情景", [item.value for item in ExecutionScenario], index=1)
        )
        cu_count = int(st.number_input("CU数量", 1, 100, 1))
        max_cu = int(st.number_input("单笔最大CU", 1, 100, 1))
        max_daily_cu = int(st.number_input("当日最大CU", 1, 10_000, 10))
        minimum_amount = float(st.number_input("最低利润金额", 0.0, 1_000_000.0, 100.0, 10.0))
        minimum_bps = float(st.number_input("最低利润（bp）", 0.0, 1_000.0, 1.0, 0.5))
        safety_bps = float(st.number_input("安全缓冲（bp）", 0.0, 1_000.0, 3.0, 0.5))
        secondary_bps = float(st.number_input("二级市场成本（bp）", 0.0, 100.0, 3.0, 0.5))
        primary_bps = float(st.number_input("申赎费（bp）", 0.0, 100.0, 0.0, 0.5))
        borrowing_bps = 0.0
        financing_bps = 0.0
        decision_latency = int(st.number_input("决策延迟（毫秒）", 0, 60_000, 50, 10))
        order_latency = int(st.number_input("订单延迟（毫秒）", 0, 60_000, 50, 10))
        primary_latency = int(st.number_input("申赎确认延迟（毫秒）", 0, 600_000, 500, 100))
        depth_haircut = float(st.slider("深度折减", 0.01, 1.0, 0.90, 0.01))
        optional_cash = st.checkbox("允许可选现金替代", value=False)
        auto_paper = st.toggle("自动影子交易", value=False)
        record_only = st.checkbox("只记录机会", value=True, disabled=not auto_paper)
        if auto_paper:
            st.warning("仅生成模拟订单，不会连接或调用真实交易柜台。")

        with st.expander("账户与一级市场情景"):
            borrowing_bps = float(st.number_input("借券成本（bp）", 0.0, 10_000.0, 0.0, 0.5))
            financing_bps = float(st.number_input("资金成本（bp）", 0.0, 10_000.0, 0.0, 0.5))
            initial_cash = float(st.number_input("初始虚拟现金", 0.0, 10_000_000_000.0, 50_000_000.0, 100_000.0))
            initial_etf_inventory = int(st.number_input("初始ETF库存", 0, 1_000_000_000, 0, 100_000))
            etf_borrow_limit = int(st.number_input("ETF虚拟券源上限", 0, 1_000_000_000, 10_000_000, 100_000))
            allow_stock_borrow = st.checkbox("允许成分股虚拟借券", value=True)
            primary_reject_probability = float(st.slider("模拟申赎拒绝概率", 0.0, 1.0, 0.0, 0.01))
            cash_component_error = float(st.number_input("最终现金差额误差", -1_000_000.0, 1_000_000.0, 0.0, 100.0))
            final_cash_delay_ms = int(st.number_input("最终退补款延迟（毫秒）", 0, 86_400_000, 1_000, 100))

        with st.expander("数据质量阈值"):
            max_age = int(st.number_input("最大报价年龄（毫秒）", 1, 600_000, 5_000))
            max_skew = int(st.number_input("最大横截面时差（毫秒）", 1, 600_000, 5_000))
            max_missing = float(st.slider("最大缺失权重", 0.0, 1.0, 0.01, 0.01))
            max_stale = float(st.slider("最大陈旧权重", 0.0, 1.0, 0.05, 0.01))
            max_suspended = float(st.slider("最大停牌权重", 0.0, 1.0, 0.05, 0.01))
            max_limit_up = float(st.slider("最大涨停无卖盘权重", 0.0, 1.0, 0.10, 0.01))
            max_limit_down = float(st.slider("最大跌停无买盘权重", 0.0, 1.0, 0.10, 0.01))
            max_iopv_error = float(st.number_input("最大IOPV误差（bp）", 0.0, 10_000.0, 30.0))
            kill_switch_enabled = st.checkbox("启用风控熔断", value=True)

    if pcf_path is None:
        st.error("缺少已校验的当日PCF。")
        return
    try:
        pcf, pcf_report = validate_executable_pcf(pcf_path, etf_code, trading_date)
    except Exception as exc:
        st.error("PCF无效：{}".format(exc))
        return
    if not pcf_report.valid:
        st.error("PCF校验未通过：{}".format(", ".join(pcf_report.errors)))
        return

    config = PaperArbitrageConfig(
        etf_code=etf_code,
        data_source=source_mode,
        redis=RedisConfig(
            enabled=redis_enabled,
            host=redis_host,
            port=redis_port,
            db=redis_db,
            password=redis_password,
            key_prefix=redis_key_prefix,
            channel_pattern=redis_channel_pattern,
            socket_timeout_seconds=redis_timeout,
        ),
        simulation=SimulationConfig(
            scenario=simulation_scenario,
            random_seed=random_seed,
            tick_interval_ms=tick_ms,
            total_ticks=total_ticks,
            simulation_speed=playback_speed,
            base_volatility=volatility,
            etf_spread_bps=etf_spread,
            component_spread_bps=component_spread,
            number_of_book_levels=levels,
            depth_per_level=depth,
            depth_decay=depth_decay,
            premium_shock_bps=premium_shock,
            shock_start_tick=shock_start_tick,
            shock_duration_ticks=shock_duration_ticks,
            mean_reversion_speed=mean_reversion_speed,
            stale_quote_ratio=stale_ratio,
            missing_quote_ratio=missing_ratio,
            suspended_weight=suspended_weight,
            limit_up_weight=limit_up_weight,
            limit_down_weight=limit_down_weight,
            quote_latency_ms=quote_latency,
            sequence_gap_probability=sequence_gap_probability,
        ),
        file_replay=FileReplayConfig(playback_speed=playback_speed),
        costs=CostConfig(
            secondary_market_bps=secondary_bps,
            creation_fee_bps=primary_bps,
            redemption_fee_bps=primary_bps,
            borrowing_bps=borrowing_bps,
            financing_bps=financing_bps,
        ),
        execution=ExecutionConfig(
            direction=direction,
            mode=execution_mode,
            scenario=execution_scenario,
            cu_count=cu_count,
            maximum_cu_per_trade=max_cu,
            maximum_daily_cu=max_daily_cu,
            decision_latency_ms=decision_latency,
            order_latency_ms=order_latency,
            primary_market_latency_ms=primary_latency,
            depth_haircut=depth_haircut,
            safety_buffer_bps=safety_bps,
            minimum_profit_bps=minimum_bps,
            minimum_profit_amount=minimum_amount,
            optional_cash_substitution=optional_cash,
            auto_paper_trade=auto_paper,
            record_opportunities_only=record_only or not auto_paper,
        ),
        quality=DataQualityConfig(
            max_quote_age_ms=max_age,
            max_cross_section_skew_ms=max_skew,
            maximum_missing_weight=max_missing,
            maximum_stale_weight=max_stale,
            maximum_suspended_weight=max_suspended,
            maximum_limit_up_weight=max_limit_up,
            maximum_limit_down_weight=max_limit_down,
            maximum_iopv_error_bps=max_iopv_error,
            kill_switch_enabled=kill_switch_enabled,
        ),
        account=AccountConfig(
            initial_cash=initial_cash,
            initial_etf_inventory=initial_etf_inventory,
            etf_borrow_limit=etf_borrow_limit,
            allow_stock_borrow=allow_stock_borrow,
        ),
        primary_market=PrimaryMarketConfig(
            rejection_probability=primary_reject_probability,
            cash_component_error=cash_component_error,
            final_cash_delay_ms=final_cash_delay_ms,
        ),
    )

    signature = _runtime_signature(Path(pcf_path), config)
    if st.session_state.get("exec_signature") != signature:
        st.session_state.exec_signature = signature
        st.session_state.exec_engine = PaperArbitrageEngine(pcf, config)
        st.session_state.exec_source = _source_for(config, pcf)
        st.session_state.exec_snapshot = None
        st.session_state.exec_result = None
        st.session_state.exec_history = _new_history()

    source = st.session_state.exec_source
    engine = st.session_state.exec_engine
    controls = st.columns(5)
    if controls[0].button("开始", use_container_width=True, disabled=source is None):
        try:
            source.start()
            _advance(source, engine)
            st.rerun()
        except Exception as exc:
            st.error("启动失败：{}".format(exc))
    if controls[1].button("暂停", use_container_width=True, disabled=source is None):
        source.stop()
        st.rerun()
    if controls[2].button("单步", use_container_width=True, disabled=source is None):
        try:
            _advance(source, engine)
            st.rerun()
        except StopIteration:
            st.info("回放已结束。")
        except Exception as exc:
            st.error("单步失败：{}".format(exc))
    if controls[3].button("恢复", use_container_width=True, disabled=source is None):
        try:
            source.start()
            _advance(source, engine)
            st.rerun()
        except Exception as exc:
            st.error("恢复失败：{}".format(exc))
    if controls[4].button("重置", use_container_width=True):
        st.session_state.pop("exec_signature", None)
        st.rerun()

    if source_mode == DataSourceMode.SIMULATED and isinstance(
        source, SimulatedMarketDataSource
    ):
        generated = min(source.current_tick, config.simulation.total_ticks)
        timeline = st.columns([1, 4, 1])
        timeline[0].metric("时间轴", "{}/{}".format(generated, config.simulation.total_ticks))
        timeline[1].progress(generated / config.simulation.total_ticks)
        timeline[2].metric("模拟速度", "{}x".format(config.simulation.simulation_speed))
        current_time = source.health().last_snapshot_time
        simulated_seconds = config.simulation.total_ticks * config.simulation.tick_interval_ms / 1_000.0
        st.caption(
            "起点 09:30:00 ｜ 当前 {} ｜ 模拟跨度 {:.1f} 秒".format(
                current_time.strftime("%H:%M:%S.%f")[:-3] if current_time else "尚未开始",
                simulated_seconds,
            )
        )

    snapshot = st.session_state.exec_snapshot
    engine_result = st.session_state.exec_result
    tabs = st.tabs(
        [
            "实时总览",
            "PCF",
            "盘口与篮子",
            "套利机会",
            "订单与申赎",
            "损益",
            "数据质量与日志",
            "历史数据与导出",
        ]
    )

    with tabs[0]:
        health = source.health() if source else None
        if snapshot is None or engine_result is None:
            st.info("点击开始或单步生成第一份快照。")
        else:
            evaluation = engine_result.decision_evaluation
            etf = snapshot.etf_order_book
            first = st.columns(6)
            system_status = (
                SYSTEM_STATUS_LABELS.get(health.status, health.status)
                if health
                else "未加载"
            )
            first[0].metric("系统状态", system_status)
            first[1].metric(
                "新交易许可",
                "允许" if evaluation.quality.new_trades_enabled else "禁止",
            )
            first[2].metric(
                "风控熔断",
                "已触发" if evaluation.quality.kill_switch else "未触发",
            )
            first[3].metric("ETF Last", "{:.4f}".format(etf.last_price or float("nan")))
            first[4].metric("ETF Bid", "{:.4f}".format(etf.best_bid or float("nan")))
            first[5].metric("ETF Ask", "{:.4f}".format(etf.best_ask or float("nan")))
            second = st.columns(6)
            second[0].metric("Official IOPV", "{:.4f}".format(evaluation.official_iopv or float("nan")))
            second[1].metric("Internal IOPV", "{:.4f}".format(evaluation.internal_iopv))
            second[2].metric("Lower Bound", "{:.4f}".format(evaluation.lower_bound))
            second[3].metric("Upper Bound", "{:.4f}".format(evaluation.upper_bound))
            second[4].metric("申购净利润", "{:,.2f}".format(evaluation.creation.net_profit))
            second[5].metric("赎回净利润", "{:,.2f}".format(evaluation.redemption.net_profit))
            figure = go.Figure()
            figure.add_bar(
                x=["Lower", "ETF Bid", "Internal IOPV", "ETF Ask", "Upper"],
                y=[evaluation.lower_bound, etf.best_bid, evaluation.internal_iopv, etf.best_ask, evaluation.upper_bound],
                marker_color=["#3A6EA5", "#4C956C", "#15616D", "#C44536", "#8B5E34"],
            )
            figure.update_layout(height=330, margin={"l": 20, "r": 20, "t": 25, "b": 20}, yaxis_title="价格")
            st.plotly_chart(figure, use_container_width=True)
            timeline_rows = pd.DataFrame(st.session_state.exec_history["snapshots"])
            if not timeline_rows.empty:
                candle_ticks = st.selectbox(
                    "K线周期",
                    [1, 5, 10, 30, 60],
                    index=1,
                    format_func=lambda value: (
                        "逐Tick" if value == 1 else "{} Tick".format(value)
                    ),
                    key="exec_candle_ticks",
                )
                candles = _candlestick_frame(timeline_rows, candle_ticks)
                candle_figure = go.Figure(
                    go.Candlestick(
                        x=candles["timestamp"],
                        open=candles["open"],
                        high=candles["high"],
                        low=candles["low"],
                        close=candles["close"],
                        name="ETF",
                        increasing_line_color="#C44536",
                        decreasing_line_color="#2F6B4F",
                    )
                )
                candle_figure.update_layout(
                    height=390,
                    margin={"l": 20, "r": 20, "t": 35, "b": 20},
                    hovermode="x unified",
                    title="ETF模拟行情K线",
                    xaxis={
                        "title": "模拟时间",
                        "rangeslider": {"visible": False},
                        "showspikes": True,
                        "spikemode": "across",
                        "spikesnap": "cursor",
                    },
                    yaxis={"title": "ETF价格", "showspikes": True},
                )
                st.plotly_chart(candle_figure, use_container_width=True)
                timeline_rows["timestamp"] = pd.to_datetime(timeline_rows["timestamp"])
                history_figure = go.Figure()
                for column, label, color in (
                    ("etf_bid", "ETF Bid", "#2F6B4F"),
                    ("etf_ask", "ETF Ask", "#C44536"),
                    ("official_iopv", "Official IOPV", "#3A6EA5"),
                    ("internal_iopv", "Internal IOPV", "#7A5C9E"),
                ):
                    history_figure.add_trace(
                        go.Scatter(
                            x=timeline_rows["timestamp"],
                            y=timeline_rows[column],
                            mode="lines",
                            name=label,
                            line={"color": color, "width": 1.5},
                        )
                    )
                history_figure.update_layout(
                    height=360,
                    margin={"l": 20, "r": 20, "t": 35, "b": 20},
                    hovermode="x unified",
                    title="模拟行情时间轴",
                    xaxis={
                        "title": "模拟时间",
                        "showspikes": True,
                        "spikemode": "across",
                        "spikesnap": "cursor",
                    },
                    yaxis={"title": "价格", "showspikes": True},
                    legend={"orientation": "h", "y": 1.08},
                )
                st.plotly_chart(history_figure, use_container_width=True)

    with tabs[1]:
        header = {
            "ETF代码": pcf.etf_code,
            "名称": pcf.symbol,
            "交易日": pcf.trading_day,
            "最小申赎单位": pcf.creation_redemption_unit,
            "成分股数量": len(pcf.components),
            "预估现金差额": pcf.estimate_cash_component,
            "最大现金替代比例": pcf.max_cash_ratio,
            "允许申购": pcf.creation_allowed,
            "允许赎回": pcf.redemption_allowed,
            "申购限额": pcf.creation_limit,
            "赎回限额": pcf.redemption_limit,
            "文件SHA256": pcf_report.file_hash,
            "校验状态": "通过" if pcf_report.valid else "失败",
        }
        st.dataframe(pd.DataFrame([header]), use_container_width=True, hide_index=True)
        subs = st.columns(3)
        subs[0].metric("禁止现金替代", pcf_report.prohibited_count)
        subs[1].metric("允许现金替代", pcf_report.optional_count)
        subs[2].metric("必须现金替代", pcf_report.mandatory_count)
        components = pd.DataFrame(
            [
                {
                    "证券代码": item.stock_code,
                    "名称": item.symbol,
                    "数量": item.component_share,
                    "替代标志": item.substitute_flag.name,
                    "申购替代金额": item.creation_cash_substitute,
                    "赎回替代金额": item.redemption_cash_substitute,
                    "申购溢价率": item.premium_ratio,
                    "赎回折价率": item.discount_ratio,
                }
                for item in pcf.components
            ]
        )
        st.dataframe(components, use_container_width=True, hide_index=True, height=430)

    with tabs[2]:
        if snapshot is None or engine_result is None:
            st.info("暂无盘口。")
        else:
            evaluation = engine_result.decision_evaluation
            left, right = st.columns([1, 2])
            with left:
                st.subheader("ETF盘口")
                st.dataframe(_book_rows(snapshot.etf_order_book), use_container_width=True, hide_index=True)
            with right:
                st.subheader("成分股盘口")
                st.dataframe(_component_book_rows(snapshot), use_container_width=True, hide_index=True, height=360)
            basket = pd.DataFrame(
                [
                    {
                        "方向": "申购",
                        "实物篮子": evaluation.creation.basket.physical_value,
                        "现金替代": evaluation.creation.basket.substitution_cash,
                        "预估现金差额": evaluation.creation.basket.estimate_cash_component,
                        "篮子合计": evaluation.creation.basket.total_value,
                        "完整成交": evaluation.creation.basket.fully_filled,
                        "瓶颈证券": evaluation.creation.basket.bottleneck_symbol,
                    },
                    {
                        "方向": "赎回",
                        "实物篮子": evaluation.redemption.basket.physical_value,
                        "现金替代": evaluation.redemption.basket.substitution_cash,
                        "预估现金差额": evaluation.redemption.basket.estimate_cash_component,
                        "篮子合计": evaluation.redemption.basket.total_value,
                        "完整成交": evaluation.redemption.basket.fully_filled,
                        "瓶颈证券": evaluation.redemption.basket.bottleneck_symbol,
                    },
                ]
            )
            st.dataframe(basket, use_container_width=True, hide_index=True)

    with tabs[3]:
        rows = st.session_state.exec_history["opportunities"]
        if rows:
            st.dataframe(pd.DataFrame(rows).tail(200), use_container_width=True, hide_index=True, height=360)
            evaluation = engine_result.decision_evaluation
            capacity = pd.DataFrame(
                [
                    asdict(evaluation.creation_capacity),
                    asdict(evaluation.redemption_capacity),
                ]
            )
            st.dataframe(capacity, use_container_width=True, hide_index=True)
            snapshots = len(st.session_state.exec_history["snapshots"])
            opportunities = pd.DataFrame(rows)
            n0 = snapshots
            n1 = int((opportunities.groupby("timestamp")["net_profit"].max() > 0).sum())
            depth_ok = opportunities["basket_fully_filled"] & opportunities["etf_fully_filled"]
            n2 = int(opportunities.loc[depth_ok, "timestamp"].nunique())
            n3 = int(opportunities[opportunities["executable"]]["timestamp"].nunique())
            n4 = len([row for row in st.session_state.exec_history["trades"] if row["fill_time"]])
            n5 = len([row for row in st.session_state.exec_history["trades"] if row["final_pnl"] > 0])
            funnel = pd.DataFrame(
                {"阶段": ["N0快照", "N1成本前正边际", "N2深度完整", "N3成本后可执行", "N4延迟后成交", "N5最终盈利"], "数量": [n0, n1, n2, n3, n4, n5]}
            )
            st.dataframe(funnel, use_container_width=True, hide_index=True)
        else:
            st.info("暂无机会记录。")

    with tabs[4]:
        history = st.session_state.exec_history
        st.subheader("模拟订单")
        st.dataframe(pd.DataFrame(history["orders"]), use_container_width=True, hide_index=True)
        st.subheader("模拟成交")
        st.dataframe(pd.DataFrame(history["fills"]), use_container_width=True, hide_index=True)
        st.subheader("一级市场申赎")
        st.dataframe(pd.DataFrame(history["primary_market_requests"]), use_container_width=True, hide_index=True)

    with tabs[5]:
        history = st.session_state.exec_history
        pnl = pd.DataFrame(history["pnl"])
        account_metrics = st.columns(4)
        account_metrics[0].metric("虚拟现金", "{:,.2f}".format(engine.account.cash))
        account_metrics[1].metric("累计已实现PnL", "{:,.2f}".format(engine.account.realized_pnl))
        account_metrics[2].metric("当日已执行CU", engine.account.daily_cu)
        account_metrics[3].metric("执行模式", execution_mode.value)
        if not pnl.empty:
            pnl["累计PnL"] = pnl["final_pnl"].cumsum()
            figure = go.Figure(
                go.Scatter(x=pd.to_datetime(pnl["timestamp"]), y=pnl["累计PnL"], mode="lines+markers", name="累计PnL")
            )
            figure.update_layout(height=350, margin={"l": 20, "r": 20, "t": 25, "b": 20}, hovermode="x unified")
            st.plotly_chart(figure, use_container_width=True)
            st.dataframe(pnl, use_container_width=True, hide_index=True)

    with tabs[6]:
        quality_rows = st.session_state.exec_history["data_quality_events"]
        if quality_rows:
            st.dataframe(pd.DataFrame(quality_rows).tail(200), use_container_width=True, hide_index=True)
        else:
            st.info("暂无数据质量记录。")
        if source:
            st.json(asdict(source.health()))

    with tabs[7]:
        summary = st.session_state.get("exec_file_summary")
        if summary:
            st.dataframe(pd.DataFrame([asdict(summary)]), use_container_width=True, hide_index=True)
        config_json = json.dumps(config.to_dict(), ensure_ascii=False, indent=2)
        st.download_button(
            "导出当前配置JSON",
            config_json.encode("utf-8"),
            file_name="executable_arbitrage_config.json",
            mime="application/json",
        )
        table = st.selectbox("导出数据表", list(st.session_state.exec_history))
        _table_download(table, st.session_state.exec_history[table])
        if st.button("保存完整运行数据", use_container_width=True):
            recorder = RunRecorder(RUN_ROOT, config.to_dict())
            for name, rows in st.session_state.exec_history.items():
                for row in rows:
                    recorder.record(name, row)
            output = recorder.save(
                {
                    "etf_code": etf_code,
                    "pcf_path": str(pcf_path),
                    "final_cash": engine.account.cash,
                    "realized_pnl": engine.account.realized_pnl,
                }
            )
            st.success("运行数据已保存：{}".format(output.relative_to(ROOT)))

    if source and source.health().running:
        interval_ms = (
            config.simulation.tick_interval_ms
            if source_mode == DataSourceMode.SIMULATED
            else config.file_replay.fixed_step_ms
        )
        time.sleep(min(5.0, max(0.01, interval_ms / 1_000.0 / playback_speed)))
        try:
            _advance(source, engine)
        except StopIteration:
            source.stop()
            st.info("回放已结束。")
        except Exception as exc:
            source.stop()
            st.error("连续回放已停止：{}".format(exc))
        else:
            st.rerun()
