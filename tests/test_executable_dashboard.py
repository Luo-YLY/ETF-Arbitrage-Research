import pandas as pd
import pytest

from etf_arbitrage.dashboard.executable_app import (
    _daily_candlestick_frame,
    _playback_refresh_plan,
    _relative_price_bps,
    _smooth_chart_payload,
)


def test_tick_history_is_aggregated_into_daily_ohlc_candles():
    rows = pd.DataFrame(
        {
            "timestamp": [
                "2026-07-22T09:30:00+08:00",
                "2026-07-22T10:00:00+08:00",
                "2026-07-23T09:30:00+08:00",
                "2026-07-23T10:00:00+08:00",
                "2026-07-23T14:00:00+08:00",
            ],
            "etf_last": [1.0, 1.3, 1.2, 1.4, 1.1],
        }
    )

    candles = _daily_candlestick_frame(rows)

    assert candles[["open", "high", "low", "close"]].to_dict("records") == [
        {"open": 1.0, "high": 1.3, "low": 1.0, "close": 1.3},
        {"open": 1.2, "high": 1.4, "low": 1.1, "close": 1.1},
    ]


def test_high_speed_playback_batches_ticks_to_reduce_page_refreshes():
    refresh_seconds, steps = _playback_refresh_plan(1_000, 100.0)
    assert refresh_seconds == 0.25
    assert steps == 25

    refresh_seconds, steps = _playback_refresh_plan(1_000, 1.0)
    assert refresh_seconds == 1.0
    assert steps == 1


def test_price_bar_uses_internal_iopv_as_zero_bps_axis():
    values = _relative_price_bps([0.998, 0.999, 1.0, 1.001, 1.002], 1.0)

    assert values == pytest.approx([-20.0, -10.0, 0.0, 10.0, 20.0])


def test_smooth_chart_payload_contains_bar_timeline_and_daily_candle():
    rows = pd.DataFrame(
        {
            "timestamp": [
                "2026-07-22T09:30:00+08:00",
                "2026-07-22T09:30:01+08:00",
            ],
            "etf_last": [1.0, 1.01],
            "etf_bid": [0.999, 1.009],
            "etf_ask": [1.001, 1.011],
            "official_iopv": [1.0, 1.005],
            "internal_iopv": [1.0, 1.006],
        }
    )

    payload = _smooth_chart_payload(
        ["下界", "ETF买一", "内部IOPV", "ETF卖一", "上界"],
        [0.998, 0.999, 1.0, 1.001, 1.002],
        1.0,
        rows,
    )

    assert payload["bar"]["values"] == pytest.approx(
        [-20.0, -10.0, 0.0, 10.0, 20.0]
    )
    assert len(payload["timeline"]["series"]) == 5
    assert payload["timeline"]["timestamps"][-1].endswith("+08:00")
    assert payload["candles"]["rows"] == [
        {
            "day": "2026-07-22",
            "open": 1.0,
            "high": 1.01,
            "low": 1.0,
            "close": 1.01,
        }
    ]
