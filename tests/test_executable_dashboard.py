import pandas as pd

from etf_arbitrage.dashboard.executable_app import (
    _daily_candlestick_frame,
    _playback_refresh_plan,
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
