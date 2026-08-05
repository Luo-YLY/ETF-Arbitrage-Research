"""Vendor-neutral order-book and executable snapshot models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from math import isfinite
from typing import Dict, Optional, Tuple


class TradingStatus(str, Enum):
    NORMAL = "NORMAL"
    SUSPENDED = "SUSPENDED"
    LIMIT_UP = "LIMIT_UP"
    LIMIT_DOWN = "LIMIT_DOWN"
    HALTED = "HALTED"
    UNKNOWN = "UNKNOWN"


class DataQualityStatus(str, Enum):
    GOOD = "GOOD"
    DEGRADED = "DEGRADED"
    INVALID = "INVALID"


@dataclass(frozen=True)
class OrderBookLevel:
    price: float
    quantity: float


@dataclass(frozen=True)
class OrderBook:
    symbol: str
    exchange: str
    exchange_timestamp: datetime
    receive_timestamp: datetime
    last_price: Optional[float]
    last_quantity: float = 0.0
    bids: Tuple[OrderBookLevel, ...] = ()
    asks: Tuple[OrderBookLevel, ...] = ()
    trading_status: TradingStatus = TradingStatus.NORMAL
    upper_limit_price: Optional[float] = None
    lower_limit_price: Optional[float] = None
    sequence_number: Optional[int] = None
    source: str = "unknown"

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0].price if self.asks else None

    @property
    def mid_price(self) -> Optional[float]:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2.0
        return self.last_price

    @property
    def has_two_sided_book(self) -> bool:
        return bool(
            self.best_bid is not None
            and self.best_ask is not None
            and self.best_bid > 0
            and self.best_ask > 0
            and self.best_bid <= self.best_ask
        )


@dataclass(frozen=True)
class FXQuote:
    """Two-sided foreign-exchange quote carried inside a market snapshot."""

    bid: float
    ask: float
    exchange_timestamp: Optional[datetime] = None
    receive_timestamp: Optional[datetime] = None
    pair: str = "HKD/CNY"
    source: str = "unknown"

    def __post_init__(self) -> None:
        pair = str(self.pair).strip().upper().replace(" ", "")
        if pair != "HKD/CNY":
            raise ValueError("Only HKD/CNY is supported by the cross-border model")
        if (
            not isfinite(self.bid)
            or not isfinite(self.ask)
            or self.bid <= 0
            or self.ask <= 0
            or self.bid > self.ask
        ):
            raise ValueError("FX bid and ask must be positive with bid <= ask")
        object.__setattr__(self, "pair", pair)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass(frozen=True)
class MarketSnapshot:
    snapshot_timestamp: datetime
    etf_order_book: OrderBook
    component_order_books: Dict[str, OrderBook] = field(default_factory=dict)
    fx_quotes: Dict[str, FXQuote] = field(default_factory=dict)
    qualified_component_order_books: Dict[str, OrderBook] = field(
        default_factory=dict
    )
    official_iopv: Optional[float] = None
    internal_iopv: Optional[float] = None
    data_quality_status: DataQualityStatus = DataQualityStatus.GOOD
    source_mode: str = "SIMULATED"
    sequence_gap: bool = False
    decode_error: Optional[str] = None
    etf_instrument_id: Optional[str] = None
    pcf_version: Optional[str] = None
    pcf_hash: Optional[str] = None
    trigger_event_id: Optional[str] = None
    event_watermark: Optional[datetime] = None
    trace_event_ids: Tuple[str, ...] = ()
    assembly_blockers: Tuple[str, ...] = ()

    @property
    def hkd_cny_quote(self) -> Optional[FXQuote]:
        return self.fx_quotes.get("HKD/CNY")


@dataclass(frozen=True)
class DataSourceHealth:
    connected: bool
    running: bool
    status: str
    last_snapshot_time: Optional[datetime] = None
    message: str = ""
