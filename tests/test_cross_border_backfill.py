from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest

from etf_arbitrage.data import (
    ETFQuote,
    JsonlSnapshotStore,
    MarketSnapshot,
    PCFComponent,
    PCFDocument,
    StockQuote,
    SubstituteFlag,
)
from etf_arbitrage.valuation import (
    backfill_cross_border_recording,
    load_hkd_cny_history,
)


def _pcf() -> PCFDocument:
    return PCFDocument(
        version="1.0",
        etf_code="159920",
        security_id_source="102",
        symbol="恒生ETF华夏",
        fund_management_company="华夏基金",
        underlying_index="HSI",
        underlying_security_id_source="103",
        creation_redemption_unit=1_000,
        estimate_cash_component=100.0,
        max_cash_ratio=1.0,
        publish=True,
        creation_allowed=True,
        redemption_allowed=True,
        record_num=1,
        total_record_num=1,
        trading_day=date(2026, 8, 4),
        previous_trading_day=date(2026, 8, 3),
        cash_component=90.0,
        nav_per_creation_unit=1_000.0,
        nav=1.0,
        components=(
            PCFComponent(
                stock_code="00001",
                security_id_source="103",
                symbol="长和",
                component_share=100.0,
                substitute_flag=SubstituteFlag.ALLOWED,
                premium_ratio=0.0,
                creation_cash_substitute=0.0,
                redemption_cash_substitute=0.0,
                exchange="HKEX",
            ),
        ),
        listing_exchange="SZSE",
        file_hash="backfill-test-pcf",
    )


def _snapshot(timestamp: datetime, etf_price: float, component_price: float):
    return MarketSnapshot(
        timestamp=timestamp,
        etf_quote=ETFQuote(
            timestamp=timestamp,
            etf_code="159920",
            last_price=etf_price,
            bid_price=None,
            ask_price=None,
            volume=1_000.0,
            amount=1_000_000.0,
        ),
        stock_quotes={
            "00001": StockQuote(
                timestamp=timestamp,
                stock_code="00001",
                last_price=component_price,
                previous_close=component_price - 0.1,
                volume=1_000.0,
            )
        },
    )


def test_post_close_fx_backfill_uses_backward_only_timestamp_match() -> None:
    recording = Path("tmp") / "tests" / "{}.jsonl".format(uuid4().hex)
    recording.parent.mkdir(parents=True, exist_ok=True)
    store = JsonlSnapshotStore(recording)
    store.append(_snapshot(datetime(2026, 8, 4, 10, 0, 30), 1.00, 10.0))
    store.append(_snapshot(datetime(2026, 8, 4, 10, 1, 30), 1.10, 11.0))
    fx = pd.DataFrame(
        {
            "fx_timestamp": pd.to_datetime(
                [
                    "2026-08-04 10:00:00",
                    "2026-08-04 10:01:00",
                    "2026-08-04 10:02:00",
                ]
            ),
            "hkd_cny_mid": [0.86, 0.87, 0.99],
        }
    )
    try:
        result = backfill_cross_border_recording(
            recording,
            _pcf(),
            fx,
            tolerance_seconds=60,
        )

        assert result["hkd_cny_mid"].tolist() == pytest.approx([0.86, 0.87])
        assert result["fx_lag_seconds"].tolist() == pytest.approx([30.0, 30.0])
        assert result["iopv"].tolist() == pytest.approx(
            [(100.0 + 100.0 * 10.0 * 0.86) / 1_000.0,
             (100.0 + 100.0 * 11.0 * 0.87) / 1_000.0]
        )
        assert set(result["valuation_status"]) == {"MODEL_IOPV_POST_CLOSE"}
        assert set(result["fx_match_direction"]) == {"BACKWARD_ONLY"}
    finally:
        recording.unlink(missing_ok=True)


def test_post_close_fx_backfill_marks_stale_rate_without_iopv() -> None:
    recording = Path("tmp") / "tests" / "{}.jsonl".format(uuid4().hex)
    recording.parent.mkdir(parents=True, exist_ok=True)
    JsonlSnapshotStore(recording).append(
        _snapshot(datetime(2026, 8, 4, 10, 5), 1.00, 10.0)
    )
    fx = pd.DataFrame(
        {
            "fx_timestamp": pd.to_datetime(["2026-08-04 10:00:00"]),
            "hkd_cny_mid": [0.86],
        }
    )
    try:
        result = backfill_cross_border_recording(
            recording,
            _pcf(),
            fx,
            tolerance_seconds=60,
        )

        assert result.iloc[0]["valuation_status"] == "STALE_FX"
        assert pd.isna(result.iloc[0]["iopv"])
        assert pd.isna(result.iloc[0]["premium"])
    finally:
        recording.unlink(missing_ok=True)


def test_load_fx_history_accepts_explicit_rate_column() -> None:
    path = Path("tmp") / "tests" / "{}_fx.csv".format(uuid4().hex)
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(
        {
            "quote_time": ["2026-08-04 10:00:00", "2026-08-04 10:01:00"],
            "mid": [0.86, 0.87],
        }
    ).to_csv(path, index=False)
    try:
        result = load_hkd_cny_history(
            path,
            timestamp_column="quote_time",
            rate_column="mid",
        )

        assert result["hkd_cny_mid"].tolist() == pytest.approx([0.86, 0.87])
        assert result["fx_timestamp"].is_monotonic_increasing
    finally:
        path.unlink(missing_ok=True)
