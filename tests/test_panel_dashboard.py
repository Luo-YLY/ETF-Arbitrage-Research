import json
from datetime import date
from pathlib import Path
from uuid import uuid4

import pandas as pd

from etf_arbitrage.dashboard_panel import (
    PanelReplayDashboard,
    discover_observation_datasets,
    load_observations,
)


def _rows():
    return [
        {
            "timestamp": "2026-07-23T09:30:00",
            "ETF_code": "159915",
            "etf_price": 3.6,
            "bid_price": None,
            "ask_price": None,
            "iopv": 3.59,
            "premium": 3.6 / 3.59 - 1,
            "valuation_quality": "good",
            "missing_weight": 0.0,
        },
        {
            "timestamp": "2026-07-23T09:30:03",
            "ETF_code": "159915",
            "etf_price": 3.61,
            "bid_price": 3.609,
            "ask_price": 3.611,
            "iopv": 3.60,
            "premium": 3.61 / 3.60 - 1,
            "valuation_quality": "good",
            "missing_weight": 0.0,
        },
        {
            "timestamp": "2026-07-23T09:30:06",
            "ETF_code": "159915",
            "etf_price": 3.605,
            "bid_price": 3.604,
            "ask_price": 3.606,
            "iopv": 3.601,
            "premium": 3.605 / 3.601 - 1,
            "valuation_quality": "good",
            "missing_weight": 0.0,
        },
    ]


def test_discovery_prefers_live_jsonl_over_generated_csv():
    root = Path("tmp") / "tests" / uuid4().hex
    live = root / "tmp" / "observations" / "20260723" / "159915.jsonl"
    replay = (
        root
        / "outputs"
        / "data_check"
        / "20260723"
        / "159915"
        / "observations.csv"
    )
    try:
        live.parent.mkdir(parents=True)
        replay.parent.mkdir(parents=True)
        live.write_text(
            "\n".join(json.dumps(row) for row in _rows()) + "\n",
            encoding="utf-8",
        )
        pd.DataFrame(_rows()).to_csv(replay, index=False)

        datasets = discover_observation_datasets(root)

        assert datasets[("20260723", "159915")].path == live
        assert datasets[("20260723", "159915")].source_kind == "live_observation"
        frame = load_observations(datasets[("20260723", "159915")])
        assert len(frame) == 3
        assert frame["timestamp"].is_monotonic_increasing
    finally:
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        root.rmdir()


def test_panel_replay_streams_rows_without_replacing_chart_models():
    root = Path("tmp") / "tests" / uuid4().hex
    live = root / "tmp" / "observations" / "20260723" / "159915.jsonl"
    try:
        live.parent.mkdir(parents=True)
        live.write_text(
            "\n".join(json.dumps(row) for row in _rows()) + "\n",
            encoding="utf-8",
        )
        dashboard = PanelReplayDashboard(root)
        dashboard.day_select.value = date(2026, 7, 23)
        source_identity = id(dashboard.source)
        template = dashboard.template()

        assert dashboard.frame["action"].tolist() == [
            "open_premium",
            "hold",
            "close_end_of_replay",
        ]
        assert dashboard.source.data["position"] == [-1, -1]
        dashboard._stream_rows(1)

        assert template.title == "深市ETF均值回复监控实验台"
        assert id(dashboard.source) == source_identity
        assert dashboard.cursor == 3
        assert len(dashboard.source.data["timestamp"]) == 3
        assert dashboard.source.data["position"] == [-1, -1, 0]
        assert dashboard.source.data["close_equity"][-1] > 0
        assert dashboard.progress.value == 100
        assert "已平仓" in dashboard.strategy_metrics.object

        dashboard._reset_clicked(None)
        assert dashboard.cursor == 2
        assert "状态：就绪" in dashboard.status.object

        dashboard._play(None)
        assert "状态：回放中" in dashboard.status.object
        dashboard._pause(None)
        assert "状态：已暂停" in dashboard.status.object
    finally:
        for path in sorted(root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        root.rmdir()
