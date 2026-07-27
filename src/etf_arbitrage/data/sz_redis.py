"""Shenzhen quotation adapters backed by a Redis hash snapshot."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, List, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from .feed import DataFeed
from .models import (
    ComponentWeight,
    ETFInfo,
    ETFQuote,
    LimitStatus,
    MarketSnapshot,
    StockQuote,
)
from .pcf import PCFDocument, SubstituteFlag


class RedisDependencyError(RuntimeError):
    """Raised when the optional redis package is not installed."""


class QuotationSchemaError(ValueError):
    """Raised when a Redis quotation record lacks required fields."""


@dataclass(frozen=True)
class SZRedisSettings:
    """Connection settings loaded explicitly or from environment variables."""

    host: str
    port: int = 6379
    password: Optional[str] = None
    db: int = 0
    socket_timeout: float = 3.0

    @classmethod
    def from_env(cls, prefix: str = "SZ_REDIS_") -> "SZRedisSettings":
        host = os.getenv(prefix + "HOST", "").strip()
        if not host:
            raise ValueError("{}HOST is required".format(prefix))
        password = os.getenv(prefix + "PASSWORD") or None
        return cls(
            host=host,
            port=int(os.getenv(prefix + "PORT", "6379")),
            password=password,
            db=int(os.getenv(prefix + "DB", "0")),
            socket_timeout=float(os.getenv(prefix + "TIMEOUT", "3.0")),
        )


class SZRedisQuotationClient:
    """Read quotation JSON records without performing writes to Redis."""

    def __init__(
        self,
        settings: SZRedisSettings,
        redis_client: Optional[Any] = None,
    ) -> None:
        self.settings = settings
        self._redis_client = redis_client

    @property
    def connection(self) -> Any:
        if self._redis_client is None:
            try:
                import redis
            except ImportError as exc:
                raise RedisDependencyError(
                    'Install Redis support with: python -m pip install -e ".[redis]"'
                ) from exc
            self._redis_client = redis.Redis(
                host=self.settings.host,
                port=self.settings.port,
                password=self.settings.password,
                db=self.settings.db,
                decode_responses=True,
                socket_connect_timeout=self.settings.socket_timeout,
                socket_timeout=self.settings.socket_timeout,
                protocol=2,
            )
        return self._redis_client

    def ping(self) -> bool:
        return bool(self.connection.ping())

    def get_security_record(
        self,
        code: str,
        trade_date: Optional[Any] = None,
    ) -> Optional[Mapping[str, Any]]:
        redis_key = self._trade_date_key(trade_date)
        raw_record = self.connection.hget(redis_key, str(code))
        if raw_record is None:
            return None
        return self._decode_record(raw_record, str(code))

    def get_quotation_snapshot(self, trade_date: Optional[Any] = None) -> pd.DataFrame:
        redis_key = self._trade_date_key(trade_date)
        codes = list(self.connection.hkeys(redis_key))
        if not codes:
            return pd.DataFrame()
        raw_records = self.connection.hmget(redis_key, codes)
        records = [
            self._decode_record(raw_record, self._as_text(code))
            for code, raw_record in zip(codes, raw_records)
            if raw_record is not None
        ]
        if not records:
            return pd.DataFrame()
        frame = pd.DataFrame(records)
        if "code" not in frame.columns:
            raise QuotationSchemaError("Redis quotation records do not contain 'code'")
        frame["code"] = frame["code"].astype(str)
        return frame.set_index("code", drop=True)

    def get_security_records(
        self,
        codes: Sequence[str],
        trade_date: Optional[Any] = None,
    ) -> pd.DataFrame:
        """Read only the requested records from the date-keyed Redis hash."""
        requested = [str(code) for code in dict.fromkeys(codes)]
        if not requested:
            return pd.DataFrame()
        redis_key = self._trade_date_key(trade_date)
        raw_records = self.connection.hmget(redis_key, requested)
        records = [
            dict(self._decode_record(raw_record, code), code=code)
            for code, raw_record in zip(requested, raw_records)
            if raw_record is not None
        ]
        if not records:
            return pd.DataFrame()
        frame = pd.DataFrame(records)
        if "code" not in frame.columns:
            raise QuotationSchemaError("Redis quotation records do not contain 'code'")
        frame["code"] = frame["code"].astype(str)
        return frame.set_index("code", drop=True)

    @staticmethod
    def _decode_record(raw_record: Any, fallback_code: str) -> Mapping[str, Any]:
        try:
            record = json.loads(SZRedisQuotationClient._as_text(raw_record))
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise QuotationSchemaError(
                "Invalid quotation JSON for {}".format(fallback_code)
            ) from exc
        if not isinstance(record, dict):
            raise QuotationSchemaError(
                "Quotation JSON for {} is not an object".format(fallback_code)
            )
        record.setdefault("code", fallback_code)
        return record

    @staticmethod
    def _as_text(value: Any) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    @staticmethod
    def _trade_date_key(value: Optional[Any]) -> str:
        if value is None:
            return date.today().strftime("%Y%m%d")
        if isinstance(value, datetime):
            return value.strftime("%Y%m%d")
        if isinstance(value, date):
            return value.strftime("%Y%m%d")
        text = str(value)
        try:
            datetime.strptime(text, "%Y%m%d")
        except ValueError as exc:
            raise ValueError("trade_date must use YYYYMMDD format") from exc
        return text


@dataclass(frozen=True)
class SZRedisFieldMap:
    """Map vendor field names to the project's canonical quote fields."""

    timestamp: Optional[str] = "timestamp"
    trade_date: Optional[str] = "cdate"
    trade_time: Optional[str] = "ctime"
    last_price: str = "closepx"
    previous_close: Optional[str] = "preClosepx"
    bid_price: Optional[str] = "bidpx1"
    ask_price: Optional[str] = "askpx1"
    volume: Optional[str] = "volume"
    amount: str = "amount"
    is_suspended: Optional[str] = None
    limit_status: Optional[str] = None


@dataclass(frozen=True)
class SZRedisPriceSeed:
    """Latest-trade price anchors used to initialize a synthetic order book."""

    trade_date: str
    etf_code: str
    etf_price: float
    component_prices: Mapping[str, float]

    @property
    def component_count(self) -> int:
        return len(self.component_prices)


def load_pcf_price_seed(
    client: SZRedisQuotationClient,
    pcf: PCFDocument,
    trade_date: Optional[Any] = None,
    redis_code_suffix: str = ".SZ",
    field_map: SZRedisFieldMap = SZRedisFieldMap(),
) -> SZRedisPriceSeed:
    """Load positive latest prices for the ETF and all physical PCF components."""
    active_components = [
        item
        for item in pcf.components
        if item.component_share > 0
        and item.substitute_flag != SubstituteFlag.MANDATORY
    ]
    suffix = redis_code_suffix.strip()

    def redis_code(code: str) -> str:
        if not suffix or code.upper().endswith(suffix.upper()):
            return code
        return code + suffix

    etf_redis_code = redis_code(pcf.etf_code)
    component_redis_codes = {
        item.stock_code: redis_code(item.stock_code) for item in active_components
    }
    requested = [etf_redis_code, *component_redis_codes.values()]
    resolved_trade_date = trade_date or pcf.trading_day
    frame = client.get_security_records(requested, resolved_trade_date)
    if frame.empty:
        raise QuotationSchemaError(
            "Redis中未找到{}的ETF及成分股行情".format(
                SZRedisQuotationClient._trade_date_key(resolved_trade_date)
            )
        )
    if etf_redis_code not in frame.index:
        raise QuotationSchemaError(
            "Redis行情缺少ETF：{}".format(etf_redis_code)
        )

    etf_price = _positive_price(
        frame.loc[etf_redis_code],
        field_map.last_price,
        etf_redis_code,
    )
    component_prices = {}
    missing = []
    for stock_code, vendor_code in component_redis_codes.items():
        if vendor_code not in frame.index:
            missing.append(vendor_code)
            continue
        component_prices[stock_code] = _positive_price(
            frame.loc[vendor_code],
            field_map.last_price,
            vendor_code,
        )
    if missing:
        preview = ", ".join(missing[:8])
        if len(missing) > 8:
            preview += " 等{}只".format(len(missing))
        raise QuotationSchemaError(
            "Redis行情缺少PCF实物成分股：{}".format(preview)
        )
    return SZRedisPriceSeed(
        trade_date=SZRedisQuotationClient._trade_date_key(resolved_trade_date),
        etf_code=pcf.etf_code,
        etf_price=etf_price,
        component_prices=component_prices,
    )


def _positive_price(row: pd.Series, field: str, code: str) -> float:
    if field not in row or pd.isna(row[field]):
        raise QuotationSchemaError(
            "Redis行情{}缺少最新价字段{}".format(code, field)
        )
    try:
        price = float(row[field])
    except (TypeError, ValueError) as exc:
        raise QuotationSchemaError(
            "Redis行情{}的{}不是数字".format(code, field)
        ) from exc
    if not np.isfinite(price) or price <= 0:
        raise QuotationSchemaError(
            "Redis行情{}的{}必须为正数，当前值为{}".format(code, field, row[field])
        )
    return price


class SZRedisDataFeed(DataFeed):
    """Convert one Redis market snapshot into the common DataFeed contract."""

    def __init__(
        self,
        client: SZRedisQuotationClient,
        etf_info: ETFInfo,
        weights: Sequence[ComponentWeight],
        field_map: SZRedisFieldMap = SZRedisFieldMap(),
        trade_date: Optional[Any] = None,
        redis_code_suffix: str = ".SZ",
        require_bid_ask: bool = True,
    ) -> None:
        self.client = client
        self._info = etf_info
        self._weights = list(weights)
        self.field_map = field_map
        self.trade_date = trade_date
        self.redis_code_suffix = redis_code_suffix
        self.require_bid_ask = require_bid_ask

    def get_etf_info(self, etf_code: str) -> ETFInfo:
        if etf_code != self._info.etf_code:
            raise KeyError("Unknown ETF: {}".format(etf_code))
        return self._info

    def get_component_weights(self, etf_code: str) -> List[ComponentWeight]:
        self.get_etf_info(etf_code)
        return list(self._weights)

    def snapshots(
        self,
        etf_code: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Iterable[MarketSnapshot]:
        self.get_etf_info(etf_code)
        redis_etf_code = self._redis_code(etf_code)
        requested_codes = [redis_etf_code] + [
            self._redis_code(component.stock_code) for component in self._weights
        ]
        frame = self.client.get_security_records(requested_codes, self.trade_date)
        if frame.empty:
            return
        if redis_etf_code not in frame.index:
            raise QuotationSchemaError(
                "ETF {} is absent from Redis snapshot".format(redis_etf_code)
            )

        etf_row = frame.loc[redis_etf_code]
        timestamp = self._timestamp(etf_row)
        if start is not None and timestamp < start:
            return
        if end is not None and timestamp > end:
            return

        stock_quotes = {}
        for component in self._weights:
            redis_stock_code = self._redis_code(component.stock_code)
            if redis_stock_code not in frame.index:
                continue
            row = frame.loc[redis_stock_code]
            stock_quotes[component.stock_code] = StockQuote(
                timestamp=self._timestamp(row, timestamp),
                stock_code=component.stock_code,
                last_price=self._optional_float(row, self.field_map.last_price),
                volume=self._optional_float(row, self.field_map.volume, 0.0) or 0.0,
                amount=self._optional_float(row, self.field_map.amount, 0.0) or 0.0,
                is_suspended=self._optional_bool(row, self.field_map.is_suspended),
                limit_status=self._limit_status(row),
                previous_close=self._optional_float(
                    row, self.field_map.previous_close
                ),
            )

        bid_price = self._optional_float(etf_row, self.field_map.bid_price)
        ask_price = self._optional_float(etf_row, self.field_map.ask_price)
        if self.require_bid_ask:
            if bid_price is None:
                raise QuotationSchemaError(
                    "Required quotation field is missing: {}".format(
                        self.field_map.bid_price or "bid_price"
                    )
                )
            if ask_price is None:
                raise QuotationSchemaError(
                    "Required quotation field is missing: {}".format(
                        self.field_map.ask_price or "ask_price"
                    )
                )

        yield MarketSnapshot(
            timestamp=timestamp,
            etf_quote=ETFQuote(
                timestamp=timestamp,
                etf_code=etf_code,
                last_price=self._required_float(etf_row, self.field_map.last_price),
                bid_price=bid_price,
                ask_price=ask_price,
                volume=self._optional_float(etf_row, self.field_map.volume, 0.0) or 0.0,
                amount=self._optional_float(etf_row, self.field_map.amount, 0.0) or 0.0,
            ),
            stock_quotes=stock_quotes,
        )

    def _redis_code(self, canonical_code: str) -> str:
        suffix = self.redis_code_suffix.strip()
        if not suffix or canonical_code.upper().endswith(suffix.upper()):
            return canonical_code
        return canonical_code + suffix

    def _timestamp(
        self,
        row: pd.Series,
        fallback: Optional[datetime] = None,
    ) -> datetime:
        field = self.field_map.timestamp
        if field and field in row and pd.notna(row[field]):
            return pd.Timestamp(row[field]).to_pydatetime()
        date_field = self.field_map.trade_date
        time_field = self.field_map.trade_time
        if (
            date_field
            and time_field
            and date_field in row
            and time_field in row
            and pd.notna(row[date_field])
            and pd.notna(row[time_field])
        ):
            return self._parse_vendor_timestamp(row[date_field], row[time_field])
        return fallback or datetime.now()

    @staticmethod
    def _parse_vendor_timestamp(trade_date: Any, trade_time: Any) -> datetime:
        date_text = str(trade_date).split(".", 1)[0]
        date_digits = "".join(character for character in date_text if character.isdigit())
        time_text = str(trade_time).split(".", 1)[0]
        time_digits = "".join(character for character in time_text if character.isdigit())
        if len(date_digits) != 8 or not time_digits:
            raise QuotationSchemaError("Invalid cdate/ctime quotation timestamp")
        time_digits = time_digits.zfill(6)
        base = datetime.strptime(date_digits + time_digits[:6], "%Y%m%d%H%M%S")
        fractional = time_digits[6:]
        if fractional:
            base = base.replace(microsecond=int((fractional + "000000")[:6]))
        return base

    @staticmethod
    def _required_float(row: pd.Series, field: str) -> float:
        value = SZRedisDataFeed._optional_float(row, field)
        if value is None:
            raise QuotationSchemaError("Required quotation field is missing: {}".format(field))
        return value

    @staticmethod
    def _optional_float(
        row: pd.Series,
        field: Optional[str],
        default: Optional[float] = None,
    ) -> Optional[float]:
        if not field or field not in row or pd.isna(row[field]):
            return default
        try:
            return float(row[field])
        except (TypeError, ValueError) as exc:
            raise QuotationSchemaError(
                "Quotation field {} is not numeric".format(field)
            ) from exc

    @staticmethod
    def _optional_bool(row: pd.Series, field: Optional[str]) -> bool:
        if not field or field not in row or pd.isna(row[field]):
            return False
        value = row[field]
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y"}
        return bool(value)

    def _limit_status(self, row: pd.Series) -> LimitStatus:
        field = self.field_map.limit_status
        if not field or field not in row or pd.isna(row[field]):
            return LimitStatus.NORMAL
        try:
            return LimitStatus(str(row[field]).lower())
        except ValueError as exc:
            raise QuotationSchemaError(
                "Unknown limit status: {}".format(row[field])
            ) from exc
