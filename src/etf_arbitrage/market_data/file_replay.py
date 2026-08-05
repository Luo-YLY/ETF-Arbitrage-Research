"""Dynamic flat-file loader and no-lookahead replay source."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
import glob
import json
from typing import Dict, Iterable, List, Mapping, Optional, Sequence

import pandas as pd

from .models import (
    DataSourceHealth,
    FXQuote,
    MarketSnapshot,
    OrderBook,
    OrderBookLevel,
    TradingStatus,
)
from .source import MarketDataSource


@dataclass(frozen=True)
class LoadSummary:
    files: tuple[str, ...]
    record_count: int
    snapshot_count: int
    symbol_count: int
    start_time: Optional[datetime]
    end_time: Optional[datetime]
    missing_fields: tuple[str, ...]
    newest_mtime: Optional[float]


class DynamicMarketDataLoader:
    REQUIRED = {"timestamp", "symbol", "is_etf"}

    def load_path(
        self, path_pattern: str, schema_mapping: Optional[Mapping[str, str]] = None
    ) -> tuple[List[MarketSnapshot], LoadSummary]:
        candidates = self._resolve(path_pattern)
        if not candidates:
            raise FileNotFoundError("No market-data files match {}".format(path_pattern))
        frames = [self._read_path(path) for path in candidates]
        frame = pd.concat(frames, ignore_index=True)
        snapshots, missing = self.from_frame(frame, schema_mapping)
        mtimes = [path.stat().st_mtime for path in candidates]
        summary = LoadSummary(
            files=tuple(str(path) for path in candidates),
            record_count=len(frame),
            snapshot_count=len(snapshots),
            symbol_count=int(frame["symbol"].nunique()) if "symbol" in frame else 0,
            start_time=snapshots[0].snapshot_timestamp if snapshots else None,
            end_time=snapshots[-1].snapshot_timestamp if snapshots else None,
            missing_fields=tuple(sorted(missing)),
            newest_mtime=max(mtimes) if mtimes else None,
        )
        return snapshots, summary

    def load_bytes(
        self,
        content: bytes,
        filename: str,
        schema_mapping: Optional[Mapping[str, str]] = None,
    ) -> tuple[List[MarketSnapshot], LoadSummary]:
        suffix = Path(filename).suffix.lower()
        if suffix == ".csv":
            frame = pd.read_csv(BytesIO(content))
        elif suffix in {".json", ".jsonl"}:
            frame = pd.read_json(BytesIO(content), lines=suffix == ".jsonl")
        elif suffix == ".parquet":
            frame = pd.read_parquet(BytesIO(content))
        else:
            raise ValueError("Unsupported market-data format: {}".format(suffix))
        snapshots, missing = self.from_frame(frame, schema_mapping)
        return snapshots, LoadSummary(
            files=(filename,),
            record_count=len(frame),
            snapshot_count=len(snapshots),
            symbol_count=int(frame["symbol"].nunique()) if "symbol" in frame else 0,
            start_time=snapshots[0].snapshot_timestamp if snapshots else None,
            end_time=snapshots[-1].snapshot_timestamp if snapshots else None,
            missing_fields=tuple(sorted(missing)),
            newest_mtime=None,
        )

    def from_frame(
        self, frame: pd.DataFrame, schema_mapping: Optional[Mapping[str, str]] = None
    ) -> tuple[List[MarketSnapshot], set[str]]:
        normalized = frame.rename(columns=dict(schema_mapping or {})).copy()
        missing = self.REQUIRED - set(normalized.columns)
        if missing:
            raise ValueError("Missing market-data columns: {}".format(sorted(missing)))
        normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], utc=True)
        normalized.sort_values(["timestamp", "is_etf", "symbol"], ascending=[True, False, True], inplace=True)
        snapshots: List[MarketSnapshot] = []
        for timestamp, group in normalized.groupby("timestamp", sort=True):
            etf_rows = group[group["is_etf"].map(self._truthy)]
            if etf_rows.empty:
                continue
            etf_book = self._book(etf_rows.iloc[0], timestamp.to_pydatetime())
            components = {
                str(row.symbol): self._book(row, timestamp.to_pydatetime())
                for row in group[~group["is_etf"].map(self._truthy)].itertuples(index=False)
            }
            first = etf_rows.iloc[0]
            fx_quote = self._fx_quote(first, timestamp.to_pydatetime())
            snapshots.append(
                MarketSnapshot(
                    snapshot_timestamp=timestamp.to_pydatetime(),
                    etf_order_book=etf_book,
                    component_order_books=components,
                    fx_quotes={"HKD/CNY": fx_quote} if fx_quote else {},
                    official_iopv=self._optional(first.get("official_iopv")),
                    internal_iopv=self._optional(first.get("internal_iopv")),
                    source_mode="FILE_REPLAY",
                    sequence_gap=bool(first.get("sequence_gap", False)),
                    decode_error=first.get("decode_error") or None,
                )
            )
        return snapshots, set()

    @classmethod
    def _fx_quote(cls, row, fallback_timestamp: datetime) -> Optional[FXQuote]:
        bid = cls._optional(row.get("hkd_cny_bid"))
        ask = cls._optional(row.get("hkd_cny_ask"))
        if bid is None or ask is None:
            return None
        exchange_value = row.get("hkd_cny_exchange_timestamp")
        receive_value = row.get("hkd_cny_receive_timestamp")
        exchange_timestamp = pd.to_datetime(
            exchange_value
            if exchange_value is not None and not pd.isna(exchange_value)
            else fallback_timestamp,
            utc=True,
        ).to_pydatetime()
        receive_timestamp = pd.to_datetime(
            receive_value
            if receive_value is not None and not pd.isna(receive_value)
            else fallback_timestamp,
            utc=True,
        ).to_pydatetime()
        source_value = row.get("hkd_cny_source", "file")
        return FXQuote(
            bid=bid,
            ask=ask,
            exchange_timestamp=exchange_timestamp,
            receive_timestamp=receive_timestamp,
            source=str(source_value or "file"),
        )

    @staticmethod
    def _resolve(pattern: str) -> List[Path]:
        path = Path(pattern)
        if path.is_dir():
            return sorted(
                item for item in path.iterdir() if item.suffix.lower() in {".csv", ".json", ".jsonl", ".parquet"}
            )
        return [Path(item) for item in sorted(glob.glob(pattern)) if Path(item).is_file()]

    @staticmethod
    def _read_path(path: Path) -> pd.DataFrame:
        suffix = path.suffix.lower()
        if suffix == ".csv":
            return pd.read_csv(path)
        if suffix == ".jsonl":
            return pd.read_json(path, lines=True)
        if suffix == ".json":
            return pd.read_json(path)
        if suffix == ".parquet":
            return pd.read_parquet(path)
        raise ValueError("Unsupported market-data format: {}".format(suffix))

    @classmethod
    def _book(cls, row, fallback_timestamp: datetime) -> OrderBook:
        getter = row.get if hasattr(row, "get") else lambda key, default=None: getattr(row, key, default)
        exchange_time = pd.to_datetime(getter("exchange_timestamp", fallback_timestamp), utc=True).to_pydatetime()
        receive_time = pd.to_datetime(getter("receive_timestamp", fallback_timestamp), utc=True).to_pydatetime()
        bids = cls._levels(getter, "bid")
        asks = cls._levels(getter, "ask")
        status_text = str(getter("trading_status", TradingStatus.NORMAL.value)).upper()
        try:
            status = TradingStatus(status_text)
        except ValueError:
            status = TradingStatus.UNKNOWN
        return OrderBook(
            symbol=str(getter("symbol")),
            exchange=str(getter("exchange", "SZSE")),
            exchange_timestamp=exchange_time,
            receive_timestamp=receive_time,
            last_price=cls._optional(getter("last_price")),
            last_quantity=float(getter("last_quantity", 0.0) or 0.0),
            bids=tuple(bids),
            asks=tuple(asks),
            trading_status=status,
            upper_limit_price=cls._optional(getter("upper_limit_price")),
            lower_limit_price=cls._optional(getter("lower_limit_price")),
            sequence_number=int(getter("sequence_number")) if cls._optional(getter("sequence_number")) is not None else None,
            source=str(getter("source", "file")),
        )

    @classmethod
    def _levels(cls, getter, prefix: str) -> List[OrderBookLevel]:
        levels = []
        for level in range(1, 11):
            price = cls._optional(getter("{}{}_price".format(prefix, level)))
            quantity = cls._optional(getter("{}{}_quantity".format(prefix, level)))
            if price is None or quantity is None:
                continue
            levels.append(OrderBookLevel(price, quantity))
        return levels

    @staticmethod
    def _optional(value) -> Optional[float]:
        if value is None or pd.isna(value):
            return None
        return float(value)

    @staticmethod
    def _truthy(value) -> bool:
        return str(value).strip().lower() in {"1", "true", "y", "yes", "etf"}


class FileReplayMarketDataSource(MarketDataSource):
    def __init__(self, snapshots: Sequence[MarketSnapshot], summary: Optional[LoadSummary] = None) -> None:
        self.snapshots = sorted(snapshots, key=lambda item: item.snapshot_timestamp)
        self.summary = summary
        self._times = [item.snapshot_timestamp for item in self.snapshots]
        self._index = -1
        self._connected = False
        self._running = False
        self._symbols: set[str] = set()

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False
        self._running = False

    def start(self) -> None:
        self.connect()
        self._running = True

    def stop(self) -> None:
        self._running = False

    def reset(self) -> None:
        self._index = -1
        self._running = False

    def subscribe(self, symbols: Iterable[str]) -> None:
        self._symbols.update(str(symbol) for symbol in symbols)

    def get_latest_snapshot(self) -> MarketSnapshot:
        if self._index < 0:
            return self.step()
        return self.snapshots[self._index]

    def step(self) -> MarketSnapshot:
        if not self.snapshots:
            raise StopIteration("File replay contains no snapshots")
        self._index += 1
        if self._index >= len(self.snapshots):
            self._index = len(self.snapshots) - 1
            self._running = False
            raise StopIteration("End of file replay")
        return self.snapshots[self._index]

    def get_at_or_before(self, decision_time: datetime) -> Optional[MarketSnapshot]:
        index = bisect_right(self._times, decision_time) - 1
        return self.snapshots[index] if index >= 0 else None

    def snapshot_at_or_after(self, timestamp: datetime) -> Optional[MarketSnapshot]:
        index = bisect_left(self._times, timestamp)
        return self.snapshots[index] if index < len(self.snapshots) else None

    def jump_to(self, timestamp: datetime) -> Optional[MarketSnapshot]:
        index = bisect_left(self._times, timestamp)
        if index >= len(self.snapshots):
            return None
        self._index = index
        return self.snapshots[index]

    def health(self) -> DataSourceHealth:
        latest = self.snapshots[self._index].snapshot_timestamp if self._index >= 0 else None
        return DataSourceHealth(
            connected=self._connected,
            running=self._running,
            status="RUNNING" if self._running else "READY",
            last_snapshot_time=latest,
            message="{} snapshots".format(len(self.snapshots)),
        )
