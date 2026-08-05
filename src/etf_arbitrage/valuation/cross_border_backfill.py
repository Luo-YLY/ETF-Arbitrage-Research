"""Post-close HKD/CNY backfill for raw cross-border ETF recordings."""

from __future__ import annotations

from bisect import bisect_right
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from etf_arbitrage.data import JsonlSnapshotStore, PCFDocument
from etf_arbitrage.domain import Exchange


FX_RATE_CANDIDATES = (
    "hkd_cny_mid",
    "hkd_cny",
    "rate",
    "closepx",
    "close",
)


def load_hkd_cny_history(
    path: Union[Path, str],
    timestamp_column: str = "timestamp",
    rate_column: Optional[str] = None,
) -> pd.DataFrame:
    """Load a timestamped HKD/CNY mid-price file without network access."""

    source = Path(path)
    suffix = source.suffix.lower()
    if suffix == ".csv":
        frame = pd.read_csv(source)
    elif suffix in {".jsonl", ".ndjson"}:
        frame = pd.read_json(source, lines=True)
    elif suffix == ".json":
        frame = pd.read_json(source)
    else:
        raise ValueError("FX文件仅支持CSV、JSON或JSONL")
    if timestamp_column not in frame.columns:
        raise ValueError("FX文件缺少时间戳列：{}".format(timestamp_column))
    resolved_rate = rate_column or next(
        (name for name in FX_RATE_CANDIDATES if name in frame.columns),
        None,
    )
    if resolved_rate is None or resolved_rate not in frame.columns:
        raise ValueError(
            "FX文件缺少HKD/CNY列；可用列名为{}，或通过--fx-rate-column指定".format(
                ", ".join(FX_RATE_CANDIDATES)
            )
        )

    normalized = pd.DataFrame(
        {
            "fx_timestamp": [
                _normalize_timestamp(value) for value in frame[timestamp_column]
            ],
            "hkd_cny_mid": pd.to_numeric(frame[resolved_rate], errors="coerce"),
        }
    )
    normalized.dropna(subset=["fx_timestamp", "hkd_cny_mid"], inplace=True)
    normalized = normalized[normalized["hkd_cny_mid"] > 0]
    normalized.sort_values("fx_timestamp", kind="stable", inplace=True)
    normalized.drop_duplicates("fx_timestamp", keep="last", inplace=True)
    normalized.reset_index(drop=True, inplace=True)
    if normalized.empty:
        raise ValueError("FX文件没有有效的正数HKD/CNY记录")
    return normalized


def backfill_cross_border_recording(
    recording_path: Union[Path, str],
    pcf: PCFDocument,
    fx_history: pd.DataFrame,
    tolerance_seconds: float = 60.0,
    fx_source: str = "POST_CLOSE_IMPORT",
) -> pd.DataFrame:
    """Build a separate MODEL_IOPV series using backward-only FX matching."""

    if tolerance_seconds < 0:
        raise ValueError("tolerance_seconds不得为负数")
    required = {"fx_timestamp", "hkd_cny_mid"}
    missing = required - set(fx_history.columns)
    if missing:
        raise ValueError("标准化FX数据缺少列：{}".format(sorted(missing)))

    fx = fx_history.copy()
    fx["fx_timestamp"] = [
        _normalize_timestamp(value) for value in fx["fx_timestamp"]
    ]
    fx["hkd_cny_mid"] = pd.to_numeric(fx["hkd_cny_mid"], errors="coerce")
    fx.dropna(subset=["fx_timestamp", "hkd_cny_mid"], inplace=True)
    fx = fx[fx["hkd_cny_mid"] > 0]
    fx.sort_values("fx_timestamp", kind="stable", inplace=True)
    fx.drop_duplicates("fx_timestamp", keep="last", inplace=True)
    if fx.empty:
        raise ValueError("没有可用于回填的HKD/CNY记录")

    fx_times = list(fx["fx_timestamp"])
    fx_rates = [float(value) for value in fx["hkd_cny_mid"]]
    rows = []
    for snapshot in JsonlSnapshotStore(recording_path).iter_snapshots():
        if snapshot.etf_quote.etf_code != pcf.etf_code:
            raise ValueError("原始快照ETF代码与PCF不一致")
        snapshot_time = _normalize_timestamp(snapshot.timestamp)
        match_index = bisect_right(fx_times, snapshot_time) - 1
        if match_index < 0:
            rows.append(_unmatched_row(snapshot, pcf, "MISSING_FX", fx_source))
            continue
        fx_time = fx_times[match_index]
        fx_rate = fx_rates[match_index]
        lag_seconds = (snapshot_time - fx_time).total_seconds()
        if lag_seconds > tolerance_seconds:
            rows.append(
                _unmatched_row(
                    snapshot,
                    pcf,
                    "STALE_FX",
                    fx_source,
                    fx_timestamp=fx_time,
                    hkd_cny_mid=fx_rate,
                    fx_lag_seconds=lag_seconds,
                )
            )
            continue

        iopv, expected, priced, missing_codes = _model_iopv(
            snapshot.stock_quotes,
            pcf,
            fx_rate,
        )
        if iopv is None:
            rows.append(
                {
                    **_base_row(snapshot, pcf, fx_source),
                    "fx_timestamp": fx_time,
                    "hkd_cny_mid": fx_rate,
                    "fx_lag_seconds": lag_seconds,
                    "iopv": float("nan"),
                    "premium": float("nan"),
                    "valuation_status": "MISSING_COMPONENT_PRICE",
                    "component_count": expected,
                    "priced_component_count": priced,
                    "missing_components": ",".join(missing_codes),
                }
            )
            continue
        rows.append(
            {
                **_base_row(snapshot, pcf, fx_source),
                "fx_timestamp": fx_time,
                "hkd_cny_mid": fx_rate,
                "fx_lag_seconds": lag_seconds,
                "iopv": iopv,
                "premium": snapshot.etf_quote.last_price / iopv - 1.0,
                "valuation_status": "MODEL_IOPV_POST_CLOSE",
                "component_count": expected,
                "priced_component_count": priced,
                "missing_components": "",
            }
        )
    return pd.DataFrame(rows)


def _model_iopv(stock_quotes, pcf: PCFDocument, hkd_cny_mid: float):
    active = [item for item in pcf.components if item.component_share > 0]
    basket_value = 0.0
    priced = 0
    missing_codes = []
    for component in active:
        quote = stock_quotes.get(component.stock_code)
        price = quote.last_price if quote is not None else None
        if price is None or price <= 0:
            missing_codes.append(component.stock_code)
            continue
        if component.instrument_id.exchange == Exchange.HKEX:
            currency_rate = hkd_cny_mid
        elif component.instrument_id.exchange in {
            Exchange.SSE,
            Exchange.SZSE,
            Exchange.BSE,
        }:
            currency_rate = 1.0
        else:
            missing_codes.append(component.stock_code)
            continue
        basket_value += component.component_share * float(price) * currency_rate
        priced += 1
    if missing_codes:
        return None, len(active), priced, missing_codes
    iopv = (
        basket_value + float(pcf.estimate_cash_component)
    ) / float(pcf.creation_redemption_unit)
    if iopv <= 0:
        return None, len(active), priced, ["NON_POSITIVE_IOPV"]
    return iopv, len(active), priced, []


def _base_row(snapshot, pcf: PCFDocument, fx_source: str) -> dict:
    return {
        "timestamp": _normalize_timestamp(snapshot.timestamp),
        "ETF_code": pcf.etf_code,
        "etf_price": snapshot.etf_quote.last_price,
        "fx_source": fx_source,
        "fx_match_direction": "BACKWARD_ONLY",
        "pcf_trade_date": pcf.trading_day.isoformat(),
        "pcf_file_hash": pcf.file_hash,
        "valuation_type": "MODEL_IOPV",
    }


def _unmatched_row(
    snapshot,
    pcf: PCFDocument,
    status: str,
    fx_source: str,
    fx_timestamp=None,
    hkd_cny_mid=float("nan"),
    fx_lag_seconds=float("nan"),
) -> dict:
    expected = sum(1 for item in pcf.components if item.component_share > 0)
    return {
        **_base_row(snapshot, pcf, fx_source),
        "fx_timestamp": fx_timestamp,
        "hkd_cny_mid": hkd_cny_mid,
        "fx_lag_seconds": fx_lag_seconds,
        "iopv": float("nan"),
        "premium": float("nan"),
        "valuation_status": status,
        "component_count": expected,
        "priced_component_count": 0,
        "missing_components": "",
    }


def _normalize_timestamp(value) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return pd.NaT
    timestamp = pd.Timestamp(timestamp)
    if timestamp.tzinfo is not None:
        timestamp = timestamp.tz_convert("Asia/Shanghai").tz_localize(None)
    return timestamp
