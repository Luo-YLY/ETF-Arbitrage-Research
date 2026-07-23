import pandas as pd

from etf_arbitrage.dashboard.executable_app import _candlestick_frame


def test_tick_history_is_aggregated_into_ohlc_candles():
    rows = pd.DataFrame(
        {
            "timestamp": pd.date_range(
                "2026-07-23 09:30:00",
                periods=5,
                freq="s",
                tz="Asia/Shanghai",
            ),
            "etf_last": [1.0, 1.3, 1.2, 1.4, 1.1],
        }
    )

    candles = _candlestick_frame(rows, ticks_per_candle=2)

    assert candles[["open", "high", "low", "close"]].to_dict("records") == [
        {"open": 1.0, "high": 1.3, "low": 1.0, "close": 1.3},
        {"open": 1.2, "high": 1.4, "low": 1.2, "close": 1.4},
        {"open": 1.1, "high": 1.1, "low": 1.1, "close": 1.1},
    ]
