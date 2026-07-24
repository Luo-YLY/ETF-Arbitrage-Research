"""Observation discovery and normalization for dashboard frontends."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Mapping, Union

import pandas as pd


REQUIRED_COLUMNS = {"timestamp", "ETF_code", "etf_price", "iopv", "premium"}
NUMERIC_COLUMNS = (
    "etf_price",
    "bid_price",
    "ask_price",
    "iopv",
    "premium",
    "missing_weight",
    "suspension_ratio",
    "limit_up_ratio",
    "limit_down_ratio",
    "risk_score",
)


@dataclass(frozen=True)
class ObservationDataset:
    trade_date: str
    etf_code: str
    path: Path
    source_kind: str


def discover_observation_datasets(
    project_root: Union[Path, str],
) -> Dict[tuple[str, str], ObservationDataset]:
    root = Path(project_root)
    datasets: Dict[tuple[str, str], ObservationDataset] = {}

    observation_root = root / "tmp" / "observations"
    for path in sorted(observation_root.glob("*/*.jsonl")):
        trade_date = path.parent.name
        etf_code = path.stem
        if _is_dataset_key(trade_date, etf_code):
            datasets[(trade_date, etf_code)] = ObservationDataset(
                trade_date=trade_date,
                etf_code=etf_code,
                path=path,
                source_kind="live_observation",
            )

    replay_root = root / "outputs" / "data_check"
    for path in sorted(replay_root.glob("*/*/observations.csv")):
        trade_date = path.parents[1].name
        etf_code = path.parent.name
        key = (trade_date, etf_code)
        if key not in datasets and _is_dataset_key(trade_date, etf_code):
            datasets[key] = ObservationDataset(
                trade_date=trade_date,
                etf_code=etf_code,
                path=path,
                source_kind="offline_replay",
            )
    return datasets


def load_observations(dataset: ObservationDataset) -> pd.DataFrame:
    if dataset.path.suffix.lower() == ".jsonl":
        frame = pd.read_json(dataset.path, lines=True)
    elif dataset.path.suffix.lower() == ".csv":
        frame = pd.read_csv(dataset.path)
    else:
        raise ValueError("Unsupported observation file: {}".format(dataset.path))

    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(
            "Observation data is missing columns: {}".format(sorted(missing))
        )

    normalized = frame.copy()
    normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], errors="coerce")
    normalized.dropna(subset=["timestamp"], inplace=True)
    normalized["ETF_code"] = (
        normalized["ETF_code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    )
    for column in NUMERIC_COLUMNS:
        if column not in normalized:
            normalized[column] = float("nan")
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")

    normalized = normalized[normalized["ETF_code"] == dataset.etf_code]
    normalized.sort_values("timestamp", kind="stable", inplace=True)
    normalized.reset_index(drop=True, inplace=True)
    if normalized.empty:
        raise ValueError("Observation dataset contains no rows for {}".format(dataset.etf_code))
    return normalized


def datasets_by_day(
    datasets: Mapping[tuple[str, str], ObservationDataset],
) -> Dict[str, list[str]]:
    values: Dict[str, list[str]] = {}
    for trade_date, etf_code in datasets:
        values.setdefault(trade_date, []).append(etf_code)
    return {
        day: sorted(set(codes))
        for day, codes in sorted(values.items(), reverse=True)
    }


def _is_dataset_key(trade_date: str, etf_code: str) -> bool:
    return (
        len(trade_date) == 8
        and trade_date.isdigit()
        and len(etf_code) == 6
        and etf_code.isdigit()
    )
