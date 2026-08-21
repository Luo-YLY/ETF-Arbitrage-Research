"""Streaming replay for full-depth executable snapshot recordings."""

from __future__ import annotations

from collections import deque
from datetime import datetime
import json
from pathlib import Path
from typing import Deque, Iterable, Mapping, Optional, TextIO

from etf_arbitrage.data import PCFDocument

from .models import (
    DataSourceHealth,
    FXQuote,
    MarketSnapshot,
    OrderBook,
    OrderBookLevel,
    TradingStatus,
)
from .source import MarketDataSource
from .state import MarketStateClassifier, optional_number


class ExecutableRecordingReplayMarketDataSource(MarketDataSource):
    """Replay the nested JSONL emitted by :class:`RedisMarketDataSource`.

    The file is streamed instead of loaded into memory. Look-ahead snapshots used
    for paper fills are buffered, so they remain available to later decision
    steps and are never skipped.
    """

    def __init__(self, path: str | Path, pcf: PCFDocument) -> None:
        self.path = Path(path).expanduser().resolve()
        self.pcf = pcf
        self.total_bytes = self._file_size()
        self.bytes_read = 0
        self.records_read = 0
        self.records_parsed = 0
        self._handle: Optional[TextIO] = None
        self._pending: Deque[MarketSnapshot] = deque()
        self._latest: Optional[MarketSnapshot] = None
        self._connected = False
        self._running = False
        self._eof = False
        self._message = ""
        self._subscribed_symbols: set[str] = set()
        self._state_classifier = MarketStateClassifier()
        self.first_snapshot = self._inspect_first_snapshot()
        self._state_classifier.reset()

    @property
    def progress_percent(self) -> int:
        if self.total_bytes <= 0:
            return 0
        return min(100, int(round(self.bytes_read / self.total_bytes * 100)))

    @property
    def buffered_records(self) -> int:
        return len(self._pending)

    def connect(self) -> None:
        if self._connected:
            return
        self._handle = self.path.open("r", encoding="utf-8")
        self._connected = True
        self._eof = False
        self._message = "完整五档采集文件已打开"

    def disconnect(self) -> None:
        if self._handle is not None:
            self._handle.close()
        self._handle = None
        self._connected = False
        self._running = False
        self._pending.clear()
        self._message = "完整五档采集文件已关闭"
        self._state_classifier.reset()

    def start(self) -> None:
        self.connect()
        self._running = True
        self._message = "完整五档采集文件回放中"

    def stop(self) -> None:
        self._running = False
        if not self._eof:
            self._message = "完整五档采集文件回放已暂停"

    def reset(self) -> None:
        if self._handle is not None:
            self._handle.close()
        self._handle = None
        self._pending.clear()
        self._latest = None
        self._connected = False
        self._running = False
        self._eof = False
        self.bytes_read = 0
        self.records_read = 0
        self.records_parsed = 0
        self._message = "完整五档采集文件回放已重置"
        self._state_classifier.reset()

    def get_latest_snapshot(self) -> MarketSnapshot:
        if self._latest is None:
            raise RuntimeError("尚未回放任何完整五档快照")
        return self._latest

    def step(self) -> MarketSnapshot:
        self.connect()
        snapshot = self._pending.popleft() if self._pending else self._read_next()
        if snapshot is None:
            self._eof = True
            self._running = False
            self._message = "完整五档采集文件回放完成"
            raise StopIteration
        self._latest = snapshot
        self.records_read += 1
        return snapshot

    def snapshot_at_or_after(self, timestamp: datetime) -> Optional[MarketSnapshot]:
        self.connect()
        if self._latest is not None and self._latest.snapshot_timestamp >= timestamp:
            return self._latest
        for snapshot in self._pending:
            if snapshot.snapshot_timestamp >= timestamp:
                return snapshot
        while True:
            snapshot = self._read_next()
            if snapshot is None:
                self._eof = True
                return None
            self._pending.append(snapshot)
            if snapshot.snapshot_timestamp >= timestamp:
                return snapshot

    def subscribe(self, symbols: Iterable[str]) -> None:
        self._subscribed_symbols.update(str(symbol) for symbol in symbols)

    def health(self) -> DataSourceHealth:
        status = (
            "RUNNING"
            if self._running
            else "COMPLETED"
            if self._eof
            else "READY"
            if self._connected
            else "DISCONNECTED"
        )
        return DataSourceHealth(
            connected=self._connected,
            running=self._running,
            status=status,
            last_snapshot_time=(
                self._latest.snapshot_timestamp if self._latest else None
            ),
            message=self._message,
        )

    def _file_size(self) -> int:
        if not self.path.exists() or not self.path.is_file():
            raise ValueError("完整五档采集文件不存在：{}".format(self.path))
        size = self.path.stat().st_size
        if size <= 0:
            raise ValueError("完整五档采集文件为空：{}".format(self.path))
        return size

    def _inspect_first_snapshot(self) -> MarketSnapshot:
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if line.strip():
                    return self._decode_line(line, line_number)
        raise ValueError("完整五档采集文件没有有效记录：{}".format(self.path))

    def _read_next(self) -> Optional[MarketSnapshot]:
        if self._handle is None:
            raise RuntimeError("完整五档采集文件尚未打开")
        while True:
            line = self._handle.readline()
            self.bytes_read = self._handle.tell()
            if line == "":
                return None
            if not line.strip():
                continue
            self.records_parsed += 1
            return self._decode_line(line, self.records_parsed)

    def _decode_line(self, line: str, line_number: int) -> MarketSnapshot:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "完整五档采集文件第{}行不是有效JSON".format(line_number)
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError(
                "完整五档采集文件第{}行schema_version必须为1".format(
                    line_number
                )
            )
        try:
            raw_records = payload.get("raw_records")
            if not isinstance(raw_records, dict):
                raw_records = {}
            etf_payload = payload["etf_order_book"]
            etf_raw = _raw_record_for(
                raw_records,
                str(etf_payload.get("symbol") or "")
                if isinstance(etf_payload, dict)
                else "",
            )
            component_payloads = payload.get("component_order_books", {})
            snapshot = MarketSnapshot(
                snapshot_timestamp=_parse_datetime(payload["snapshot_timestamp"]),
                etf_order_book=_decode_book(etf_payload, etf_raw),
                component_order_books={
                    str(symbol): _decode_book(
                        book,
                        _raw_record_for(raw_records, str(symbol)),
                    )
                    for symbol, book in component_payloads.items()
                },
                fx_quotes={
                    str(pair).upper(): _decode_fx_quote(str(pair), quote)
                    for pair, quote in payload.get("fx_quotes", {}).items()
                },
                source_mode=str(payload.get("source_mode") or "REDIS_DATE_HASH"),
                pcf_version=(
                    str(payload["pcf_version"])
                    if payload.get("pcf_version") is not None
                    else None
                ),
                pcf_hash=(
                    str(payload["pcf_hash"])
                    if payload.get("pcf_hash") is not None
                    else None
                ),
            )
            snapshot = self._state_classifier.classify_snapshot(snapshot)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                "完整五档采集文件第{}行字段无效：{}".format(
                    line_number, exc
                )
            ) from exc
        self._validate_identity(snapshot, line_number)
        return snapshot

    def _validate_identity(self, snapshot: MarketSnapshot, line_number: int) -> None:
        actual_code = snapshot.etf_order_book.symbol.split(".", 1)[0].zfill(6)
        if actual_code != self.pcf.etf_code.zfill(6):
            raise ValueError(
                "完整五档采集文件第{}行ETF为{}，与PCF {}不一致".format(
                    line_number, actual_code, self.pcf.etf_code
                )
            )
        if snapshot.snapshot_timestamp.date() != self.pcf.trading_day:
            raise ValueError(
                "完整五档采集文件第{}行交易日为{}，与PCF {}不一致".format(
                    line_number,
                    snapshot.snapshot_timestamp.date(),
                    self.pcf.trading_day,
                )
            )
        if (
            snapshot.pcf_hash
            and self.pcf.file_hash
            and snapshot.pcf_hash != self.pcf.file_hash
        ):
            raise ValueError(
                (
                    "完整五档采集文件第{}行PCF哈希与当前PCF不一致"
                    "（采集文件={}，当前PCF={}）。请从采集设备复制该交易日"
                    "实际使用的原始PCF；不要修改采集记录中的哈希。"
                ).format(
                    line_number,
                    snapshot.pcf_hash,
                    self.pcf.file_hash,
                )
            )
        if (
            snapshot.pcf_version
            and self.pcf.version
            and snapshot.pcf_version != self.pcf.version
        ):
            raise ValueError(
                "完整五档采集文件第{}行PCF版本与当前PCF不一致".format(
                    line_number
                )
            )


def _parse_datetime(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("时间戳缺失")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _decode_book(
    payload: object,
    raw_record: Optional[Mapping[str, object]] = None,
) -> OrderBook:
    if not isinstance(payload, dict):
        raise ValueError("盘口必须是对象")
    raw_record = raw_record or {}
    status_text = str(payload.get("status") or TradingStatus.UNKNOWN.value)
    try:
        status = TradingStatus(status_text)
    except ValueError:
        status = TradingStatus.UNKNOWN
    return OrderBook(
        symbol=str(payload["symbol"]),
        exchange=str(payload.get("exchange") or ""),
        exchange_timestamp=_parse_datetime(payload["exchange_timestamp"]),
        receive_timestamp=_parse_datetime(payload["receive_timestamp"]),
        last_price=(
            float(payload["last_price"])
            if payload.get("last_price") is not None
            else None
        ),
        bids=_decode_levels(payload.get("bids"), "买盘"),
        asks=_decode_levels(payload.get("asks"), "卖盘"),
        trading_status=status,
        raw_status=str(
            payload.get("raw_status")
            or raw_record.get("status")
            or ""
        ),
        previous_close=(
            float(payload["previous_close"])
            if payload.get("previous_close") is not None
            else optional_number(
                raw_record,
                "preClosepx",
                "preClosePrice",
                "previousClose",
            )
        ),
        cumulative_amount=(
            float(payload["cumulative_amount"])
            if payload.get("cumulative_amount") is not None
            else optional_number(raw_record, "amount", "turnover")
        ),
        cumulative_volume=(
            float(payload["cumulative_volume"])
            if payload.get("cumulative_volume") is not None
            else optional_number(raw_record, "volume", "balance")
        ),
        upper_limit_price=(
            float(payload["upper_limit_price"])
            if payload.get("upper_limit_price") is not None
            else optional_number(
                raw_record,
                "upperLimitPrice",
                "upperLimitPx",
                "highLimitPrice",
            )
        ),
        lower_limit_price=(
            float(payload["lower_limit_price"])
            if payload.get("lower_limit_price") is not None
            else optional_number(
                raw_record,
                "lowerLimitPrice",
                "lowerLimitPx",
                "lowLimitPrice",
            )
        ),
        source=str(payload.get("source") or "executable_recording"),
    )


def _raw_record_for(
    records: Mapping[str, object],
    symbol: str,
) -> Mapping[str, object]:
    code = str(symbol).split(".", 1)[0].zfill(6)
    for key, value in records.items():
        if str(key).split(".", 1)[0].zfill(6) == code and isinstance(value, dict):
            return value
    return {}


def _decode_levels(payload: object, label: str) -> tuple[OrderBookLevel, ...]:
    if payload is None:
        return ()
    if not isinstance(payload, list):
        raise ValueError("{}档位必须是数组".format(label))
    levels = []
    for level in payload:
        if not isinstance(level, (list, tuple)) or len(level) != 2:
            raise ValueError("{}档位必须为[价格,数量]".format(label))
        levels.append(OrderBookLevel(float(level[0]), float(level[1])))
    return tuple(levels)


def _decode_fx_quote(pair: str, payload: object) -> FXQuote:
    if not isinstance(payload, dict):
        raise ValueError("汇率快照必须是对象")
    return FXQuote(
        pair=pair,
        bid=float(payload["bid"]),
        ask=float(payload["ask"]),
        exchange_timestamp=(
            _parse_datetime(payload["exchange_timestamp"])
            if payload.get("exchange_timestamp")
            else None
        ),
        receive_timestamp=(
            _parse_datetime(payload["receive_timestamp"])
            if payload.get("receive_timestamp")
            else None
        ),
        source=str(payload.get("source") or "executable_recording"),
    )
