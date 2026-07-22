from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from etf_arbitrage.backtest import PremiumBacktester, ResearchReplay
from etf_arbitrage.data import (
    ComponentWeight,
    DataFrameReplayFeed,
    ETFInfo,
    ETFQuote,
    JsonlSnapshotStore,
    MarketSnapshot,
    StockQuote,
)


def snapshot(timestamp: datetime, price: float = 1.2) -> MarketSnapshot:
    return MarketSnapshot(
        timestamp=timestamp,
        etf_quote=ETFQuote(timestamp, "159915", price, None, None, 0, 10_000_000),
        stock_quotes={
            "000001": StockQuote(timestamp, "000001", 1.0, 0, amount=5_000_000)
        },
    )


def test_jsonl_store_deduplicates_and_loads_replay_frames() -> None:
    path = Path("tmp") / "tests" / "{}.jsonl".format(uuid4().hex)
    try:
        store = JsonlSnapshotStore(path)
        first = snapshot(datetime(2026, 7, 22, 9, 30))
        second = snapshot(datetime(2026, 7, 22, 9, 30) + timedelta(seconds=3), 1.201)

        assert store.append(first)
        assert not store.append(first, captured_at=datetime(2026, 7, 22, 9, 31))
        assert store.append(second)

        etf_frame, stock_frame = JsonlSnapshotStore(path).to_frames("159915")

        assert len(etf_frame) == 2
        assert len(stock_frame) == 2
        assert etf_frame.iloc[0]["bid_price"] is None
        assert etf_frame.iloc[-1]["last_price"] == pytest.approx(1.201)
    finally:
        path.unlink(missing_ok=True)


def test_recorded_snapshots_run_through_indicative_replay_and_backtest() -> None:
    path = Path("tmp") / "tests" / "{}.jsonl".format(uuid4().hex)
    start = datetime(2026, 7, 22, 10, 0)
    try:
        store = JsonlSnapshotStore(path)
        for index, price in enumerate([1.0, 1.006, 1.004, 1.0005]):
            store.append(snapshot(start + timedelta(minutes=index), price))
        etf_frame, stock_frame = store.to_frames("159915")
        info = ETFInfo("159915", "创业板ETF", "SZSE", "创业板指", 1, 1)
        weights = [ComponentWeight("159915", "000001", 1.0)]
        observations = ResearchReplay(
            DataFrameReplayFeed(info, weights, etf_frame, stock_frame)
        ).run("159915")
        observations["risk_blocked"] = observations["indicative_risk_blocked"]

        result = PremiumBacktester().run(observations)

        assert len(observations) == 4
        assert not observations["has_executable_quote"].any()
        assert len(result.trades) == 1
        assert result.trades[0].direction == "premium"
    finally:
        path.unlink(missing_ok=True)
