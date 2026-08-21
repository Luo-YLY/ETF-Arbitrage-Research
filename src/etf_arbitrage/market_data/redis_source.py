"""Read-only Redis adapters for normalized and raw quotation snapshots."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from math import isfinite
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from etf_arbitrage.data.pcf import PCFDocument
from etf_arbitrage.domain import Exchange, InstrumentId, vendor_symbol
from etf_arbitrage.executable_config import RedisConfig, RedisSnapshotFormat

from .file_replay import DynamicMarketDataLoader
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


class NoNewSnapshotError(RuntimeError):
    """Raised when Redis still contains the last snapshot already processed."""


class RedisMarketDataSource(MarketDataSource):
    """Read Redis market snapshots without exposing any trading operation.

    ``NORMALIZED_JSON`` retains the original ``<prefix>:snapshot:<ETF>``
    contract. ``DATE_HASH`` reads the intranet date-keyed Redis hash shown by
    the quotation probe and converts its five-level fields into the common
    executable-order-book model.
    """

    def __init__(
        self,
        config: RedisConfig,
        etf_code: str,
        pcf: Optional[PCFDocument] = None,
        redis_client: Optional[Any] = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.config = config
        self.etf_code = etf_code
        self.pcf = pcf
        self._client = redis_client
        self._clock = clock
        self._connected = False
        self._running = False
        self._latest: Optional[MarketSnapshot] = None
        self._latest_fingerprint = self._load_recording_fingerprint()
        self._message = "Redis disabled" if not config.enabled else "not connected"
        self._symbols: set[str] = set()
        self._state_classifier = MarketStateClassifier()

    def connect(self) -> None:
        if not self.config.enabled:
            self._message = "Redis is disabled by configuration"
            return
        try:
            if self._client is None:
                import redis  # type: ignore

                self._client = redis.Redis(
                    host=self.config.host,
                    port=self.config.port,
                    db=self.config.db,
                    password=self.config.password,
                    socket_timeout=self.config.socket_timeout_seconds,
                    socket_connect_timeout=self.config.socket_timeout_seconds,
                    decode_responses=False,
                    protocol=2,
                )
            self._client.ping()
            self._connected = True
            self._message = "connected ({})".format(self.config.snapshot_format.value)
        except Exception as exc:
            self._client = None
            self._connected = False
            self._message = "{}: {}".format(type(exc).__name__, exc)

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        self._connected = False
        self._running = False
        self._state_classifier.reset()

    def start(self) -> None:
        if not self._connected:
            self.connect()
        self._running = self._connected

    def stop(self) -> None:
        self._running = False

    def subscribe(self, symbols: Iterable[str]) -> None:
        self._symbols.update(str(symbol) for symbol in symbols)

    def get_latest_snapshot(self) -> MarketSnapshot:
        return self.step()

    def step(self) -> MarketSnapshot:
        if not self.config.enabled:
            raise RuntimeError("Redis source is disabled")
        if not self._connected or self._client is None:
            self.connect()
        if not self._connected or self._client is None:
            raise ConnectionError(self._message)
        try:
            if self.config.snapshot_format == RedisSnapshotFormat.DATE_HASH:
                snapshot = self._read_date_hash()
            else:
                snapshot = self._read_normalized_json()
            self._latest = snapshot
            self._message = "connected; latest={}".format(
                snapshot.snapshot_timestamp.isoformat()
            )
            return snapshot
        except NoNewSnapshotError:
            self._message = "connected; waiting for a new Redis snapshot"
            raise
        except Exception as exc:
            self._message = "{}: {}".format(type(exc).__name__, exc)
            raise

    def _read_normalized_json(self) -> MarketSnapshot:
        key = "{}:snapshot:{}".format(self.config.key_prefix, self.etf_code)
        raw = self._client.get(key)
        if not raw:
            raise KeyError("Redis snapshot key is missing: {}".format(key))
        payload = json.loads(self._as_text(raw))
        frame_rows = payload.get("rows") if isinstance(payload, dict) else payload
        if not isinstance(frame_rows, list):
            raise ValueError("Normalized Redis payload must contain a rows list")
        import pandas as pd

        snapshots, _ = DynamicMarketDataLoader().from_frame(pd.DataFrame(frame_rows))
        if not snapshots:
            raise ValueError("Normalized Redis payload contains no complete snapshot")
        return self._state_classifier.classify_snapshot(snapshots[-1])

    def _read_date_hash(self) -> MarketSnapshot:
        if self.pcf is None:
            raise ValueError("DATE_HASH Redis mode requires a validated PCF")
        key = self.config.trade_date_key or self.pcf.trading_day.strftime("%Y%m%d")
        if len(key) != 8 or not key.isdigit():
            raise ValueError("Redis trade_date_key must use YYYYMMDD")

        active_components = [
            item
            for item in self.pcf.components
            if item.component_share > 0
            and not item.substitute_flag.requires_cash_substitution
        ]
        etf_vendor_code = vendor_symbol(self.pcf.etf_id)
        component_codes = {
            item.stock_code: vendor_symbol(item.instrument_id)
            for item in active_components
        }
        requested = [etf_vendor_code, *component_codes.values()]
        hkd_cny_code = self.config.hkd_cny_code.strip()
        if hkd_cny_code:
            requested.append(hkd_cny_code)
        raw_values = self._client.hmget(key, requested)
        records = {
            code: self._decode_record(raw, code)
            for code, raw in zip(requested, raw_values)
            if raw is not None
        }
        if etf_vendor_code not in records:
            raise KeyError(
                "Redis hash {} is missing ETF field {}".format(key, etf_vendor_code)
            )

        fingerprint = self._fingerprint(records)
        if fingerprint == self._latest_fingerprint:
            raise NoNewSnapshotError("Redis snapshot is unchanged")

        receive_time = self._clock()
        etf_book = self._order_book(
            records[etf_vendor_code],
            self.pcf.etf_id,
            receive_time,
        )
        component_books = {}
        qualified_books = {}
        missing_vendor_codes = []
        for item in active_components:
            vendor_code = component_codes[item.stock_code]
            record = records.get(vendor_code)
            if record is None:
                missing_vendor_codes.append(vendor_code)
                continue
            book = self._order_book(record, item.instrument_id, receive_time)
            component_books[item.stock_code] = book
            qualified_books[item.instrument_id.key] = book

        fx_quotes = {}
        if hkd_cny_code and hkd_cny_code in records:
            fx_record = records[hkd_cny_code]
            fx_instrument = InstrumentId(Exchange.FX, hkd_cny_code)
            fx_book = self._order_book(fx_record, fx_instrument, receive_time)
            if fx_book.best_bid is not None and fx_book.best_ask is not None:
                fx_quotes["HKD/CNY"] = FXQuote(
                    bid=fx_book.best_bid,
                    ask=fx_book.best_ask,
                    exchange_timestamp=fx_book.exchange_timestamp,
                    receive_timestamp=receive_time,
                    source="REDIS_DATE_HASH",
                )

        event_times = [
            etf_book.exchange_timestamp,
            *(book.exchange_timestamp for book in component_books.values()),
            *(
                quote.exchange_timestamp
                for quote in fx_quotes.values()
                if quote.exchange_timestamp is not None
            ),
        ]
        watermark = max(event_times) if event_times else None
        snapshot = MarketSnapshot(
            snapshot_timestamp=receive_time,
            etf_order_book=etf_book,
            component_order_books=component_books,
            qualified_component_order_books=qualified_books,
            fx_quotes=fx_quotes,
            source_mode="REDIS_DATE_HASH",
            etf_instrument_id=self.pcf.etf_id.key,
            pcf_version=self.pcf.version,
            pcf_hash=self.pcf.file_hash,
            event_watermark=watermark,
            assembly_blockers=(
                ("MISSING_RAW_COMPONENT_RECORDS:{}".format(len(missing_vendor_codes)),)
                if missing_vendor_codes
                else ()
            ),
        )
        snapshot = self._state_classifier.classify_snapshot(snapshot)
        self._append_recording(
            snapshot,
            records,
            key,
            receive_time,
            fingerprint,
        )
        self._latest_fingerprint = fingerprint
        return snapshot

    def _order_book(
        self,
        record: Mapping[str, Any],
        instrument_id: InstrumentId,
        receive_time: datetime,
    ) -> OrderBook:
        levels = max(1, min(int(self.config.number_of_book_levels), 10))
        bids = self._levels(record, "bid", levels)
        asks = self._levels(record, "ask", levels)
        return OrderBook(
            symbol=instrument_id.code,
            exchange=self._exchange(record, instrument_id.exchange),
            exchange_timestamp=self._record_timestamp(record, receive_time),
            receive_timestamp=receive_time,
            last_price=self._optional_number(record, ("closepx", "lastPrice")),
            last_quantity=0.0,
            bids=bids,
            asks=asks,
            trading_status=self._trading_status(record.get("status")),
            raw_status=str(record.get("status") or ""),
            previous_close=optional_number(
                record,
                "preClosepx",
                "preClosePrice",
                "previousClose",
            ),
            cumulative_amount=optional_number(record, "amount", "turnover"),
            cumulative_volume=optional_number(record, "volume", "balance"),
            upper_limit_price=optional_number(
                record,
                "upperLimitPrice",
                "upperLimitPx",
                "highLimitPrice",
            ),
            lower_limit_price=optional_number(
                record,
                "lowerLimitPrice",
                "lowerLimitPx",
                "lowLimitPrice",
            ),
            source="REDIS_DATE_HASH",
        )

    @classmethod
    def _levels(
        cls,
        record: Mapping[str, Any],
        side: str,
        maximum: int,
    ) -> tuple[OrderBookLevel, ...]:
        output = []
        for level in range(1, maximum + 1):
            if side == "bid":
                price_fields = (
                    "bidPrice{}".format(level),
                    "bidpx{}".format(level),
                )
                quantity_fields = (
                    "bidVolume{}".format(level),
                    "bidvolume{}".format(level),
                    "bidVol{}".format(level),
                )
            else:
                price_fields = (
                    "offerPrice{}".format(level),
                    "askPrice{}".format(level),
                    "askpx{}".format(level),
                )
                quantity_fields = (
                    "offerVolume{}".format(level),
                    "askVolume{}".format(level),
                    "askvolume{}".format(level),
                )
            price = cls._optional_number(record, price_fields)
            quantity = cls._optional_number(record, quantity_fields)
            if price is None or quantity is None or price <= 0 or quantity <= 0:
                continue
            output.append(OrderBookLevel(price=price, quantity=quantity))
        output.sort(key=lambda item: item.price, reverse=side == "bid")
        return tuple(output)

    @staticmethod
    def _exchange(record: Mapping[str, Any], fallback: Exchange) -> str:
        market = str(record.get("market") or "").strip().upper()
        return {
            "SZ": Exchange.SZSE.value,
            "SZSE": Exchange.SZSE.value,
            "SH": Exchange.SSE.value,
            "SSE": Exchange.SSE.value,
            "HK": Exchange.HKEX.value,
            "HKEX": Exchange.HKEX.value,
            "FX": Exchange.FX.value,
        }.get(market, fallback.value)

    @staticmethod
    def _trading_status(value: Any) -> TradingStatus:
        text = str(value or "").strip().upper()
        if text in {"", "0", "E0", "T0", "T111", "NORMAL", "TRADING"}:
            return TradingStatus.NORMAL
        if text in {"S", "SUSPENDED", "SUSPEND"}:
            return TradingStatus.SUSPENDED
        if text in {"HALTED", "HALT"}:
            return TradingStatus.HALTED
        if text in {"LIMIT_UP", "UP_LIMIT"}:
            return TradingStatus.LIMIT_UP
        if text in {"LIMIT_DOWN", "DOWN_LIMIT"}:
            return TradingStatus.LIMIT_DOWN
        return TradingStatus.UNKNOWN

    @classmethod
    def _record_timestamp(
        cls,
        record: Mapping[str, Any],
        receive_time: datetime,
    ) -> datetime:
        explicit = record.get("timestamp")
        if explicit not in (None, ""):
            parsed = datetime.fromisoformat(str(explicit).replace("Z", "+00:00"))
            return cls._align_timezone(parsed, receive_time)
        date_digits = "".join(
            character for character in str(record.get("cdate") or "") if character.isdigit()
        )
        time_digits = "".join(
            character for character in str(record.get("ctime") or "") if character.isdigit()
        )
        if len(date_digits) != 8 or not time_digits:
            return receive_time
        time_digits = time_digits.zfill(6)
        parsed = datetime.strptime(date_digits + time_digits[:6], "%Y%m%d%H%M%S")
        fractional = time_digits[6:]
        if fractional:
            parsed = parsed.replace(microsecond=int((fractional + "000000")[:6]))
        return cls._align_timezone(parsed, receive_time)

    @staticmethod
    def _align_timezone(value: datetime, reference: datetime) -> datetime:
        if reference.tzinfo is None:
            return value.replace(tzinfo=None)
        if value.tzinfo is None:
            return value.replace(tzinfo=reference.tzinfo)
        return value.astimezone(reference.tzinfo)

    @staticmethod
    def _optional_number(
        record: Mapping[str, Any],
        fields: Iterable[str],
    ) -> Optional[float]:
        for field in fields:
            value = record.get(field)
            if value in (None, ""):
                continue
            try:
                number = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("Redis quotation field {} is not numeric".format(field)) from exc
            if not isfinite(number):
                raise ValueError("Redis quotation field {} is not finite".format(field))
            return number
        return None

    @classmethod
    def _decode_record(cls, raw: Any, fallback_code: str) -> Mapping[str, Any]:
        try:
            record = json.loads(cls._as_text(raw))
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Invalid Redis quotation JSON for {}".format(fallback_code)) from exc
        if not isinstance(record, dict):
            raise ValueError("Redis quotation JSON for {} is not an object".format(fallback_code))
        record.setdefault("code", fallback_code)
        return record

    def _append_recording(
        self,
        snapshot: MarketSnapshot,
        raw_records: Mapping[str, Mapping[str, Any]],
        redis_key: str,
        captured_at: datetime,
        fingerprint: str,
    ) -> None:
        if not self.config.recording_path:
            return
        path = Path(self.config.recording_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "fingerprint": fingerprint,
            "captured_at": captured_at.isoformat(),
            "redis_key": redis_key,
            "snapshot_timestamp": snapshot.snapshot_timestamp.isoformat(),
            "source_mode": snapshot.source_mode,
            "pcf_version": snapshot.pcf_version,
            "pcf_hash": snapshot.pcf_hash,
            "etf_order_book": self._book_payload(snapshot.etf_order_book),
            "component_order_books": {
                symbol: self._book_payload(book)
                for symbol, book in snapshot.component_order_books.items()
            },
            "fx_quotes": {
                pair: {
                    "bid": quote.bid,
                    "ask": quote.ask,
                    "exchange_timestamp": (
                        quote.exchange_timestamp.isoformat()
                        if quote.exchange_timestamp
                        else None
                    ),
                    "receive_timestamp": (
                        quote.receive_timestamp.isoformat()
                        if quote.receive_timestamp
                        else None
                    ),
                    "source": quote.source,
                }
                for pair, quote in snapshot.fx_quotes.items()
            },
            "raw_records": raw_records,
        }
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
        fingerprint_path = self._fingerprint_path(path)
        temporary = fingerprint_path.with_suffix(fingerprint_path.suffix + ".tmp")
        temporary.write_text(fingerprint, encoding="ascii")
        temporary.replace(fingerprint_path)

    def _load_recording_fingerprint(self) -> Optional[str]:
        if not self.config.recording_path:
            return None
        recording_path = Path(self.config.recording_path)
        try:
            if not recording_path.exists() or recording_path.stat().st_size <= 0:
                return None
        except OSError:
            return None
        fingerprint_path = self._fingerprint_path(recording_path)
        try:
            value = fingerprint_path.read_text(encoding="ascii").strip().lower()
        except OSError:
            return None
        if len(value) == 64 and all(character in "0123456789abcdef" for character in value):
            return value
        return None

    @staticmethod
    def _fingerprint_path(recording_path: Path) -> Path:
        return recording_path.with_name(recording_path.name + ".fingerprint")

    @staticmethod
    def _book_payload(book: OrderBook) -> dict[str, Any]:
        return {
            "symbol": book.symbol,
            "exchange": book.exchange,
            "exchange_timestamp": book.exchange_timestamp.isoformat(),
            "receive_timestamp": book.receive_timestamp.isoformat(),
            "last_price": book.last_price,
            "status": book.trading_status.value,
            "raw_status": book.raw_status,
            "previous_close": book.previous_close,
            "cumulative_amount": book.cumulative_amount,
            "cumulative_volume": book.cumulative_volume,
            "upper_limit_price": book.upper_limit_price,
            "lower_limit_price": book.lower_limit_price,
            "market_phase": book.market_phase.value,
            "instrument_state": book.instrument_state.value,
            "state_confidence": book.state_confidence.value,
            "state_reasons": list(book.state_reasons),
            "quote_inactivity_age_ms": book.quote_inactivity_age_ms,
            "price_limit_source": book.price_limit_source,
            "bids": [[level.price, level.quantity] for level in book.bids],
            "asks": [[level.price, level.quantity] for level in book.asks],
            "source": book.source,
        }

    @staticmethod
    def _fingerprint(records: Mapping[str, Mapping[str, Any]]) -> str:
        encoded = json.dumps(
            records,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _as_text(value: Any) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    def health(self) -> DataSourceHealth:
        return DataSourceHealth(
            connected=self._connected,
            running=self._running,
            status=(
                "DISABLED"
                if not self.config.enabled
                else "RUNNING"
                if self._running
                else "READY"
                if self._connected
                else "ERROR"
            ),
            last_snapshot_time=self._latest.snapshot_timestamp if self._latest else None,
            message=self._message,
        )
