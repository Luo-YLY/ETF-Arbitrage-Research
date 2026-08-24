"""Adapter exposing the existing Shenzhen implementation through new contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

from etf_arbitrage.data import SZSEPCFParser
from etf_arbitrage.domain import EventEnvelope, EventType, Exchange, InstrumentId
from etf_arbitrage.market_data import OrderBook
from etf_arbitrage.operations import SZSEMarketSchedule

from .base import ExchangeAdapter, ExchangeCapabilities, ExchangeCalendar, PCFParser


@dataclass(frozen=True)
class SZSEInstrumentCodec:
    suffix: str = ".SZ"

    def decode(self, vendor_code: str) -> InstrumentId:
        value = str(vendor_code).strip().upper()
        suffix = self.suffix.upper()
        if suffix and value.endswith(suffix):
            value = value[: -len(suffix)]
        return InstrumentId(Exchange.SZSE, value)

    def encode(self, instrument_id: InstrumentId) -> str:
        if instrument_id.exchange != Exchange.SZSE:
            raise ValueError("SZSE codec cannot encode {}".format(instrument_id))
        return "{}{}".format(instrument_id.code, self.suffix)


class SZSEAdapter(ExchangeAdapter):
    exchange = Exchange.SZSE
    capabilities = ExchangeCapabilities(
        pcf_parser=True,
        instrument_codec=True,
        exchange_calendar=True,
        market_data_events=True,
        rule_book=False,
        settlement_model=False,
        production_ready=False,
        notes=(
            "Wraps the existing SZSE PCF parser and continuous-auction calendar.",
            "Market-data events require already normalized OrderBook values.",
            "No broker, account or settlement adapter is connected.",
        ),
    )

    def __init__(self, suffix: str = ".SZ") -> None:
        self._codec = SZSEInstrumentCodec(suffix=suffix)
        self._pcf_parser = SZSEPCFParser()
        self._calendar = SZSEMarketSchedule()

    @property
    def codec(self) -> SZSEInstrumentCodec:
        return self._codec

    @property
    def pcf_parser(self) -> PCFParser:
        return self._pcf_parser

    @property
    def calendar(self) -> ExchangeCalendar:
        return self._calendar

    def order_book_event(
        self,
        book: OrderBook,
        trade_date,
        received_time: Optional[datetime] = None,
        correlation_id: Optional[str] = None,
    ) -> EventEnvelope:
        instrument_id = self.codec.decode(book.symbol)
        payload: Mapping[str, Any] = {
            "symbol": book.symbol,
            "exchange": book.exchange,
            "exchange_timestamp": book.exchange_timestamp.isoformat(),
            "receive_timestamp": book.receive_timestamp.isoformat(),
            "last_price": book.last_price,
            "last_quantity": book.last_quantity,
            "bids": [
                {"price": level.price, "quantity": level.quantity}
                for level in book.bids
            ],
            "asks": [
                {"price": level.price, "quantity": level.quantity}
                for level in book.asks
            ],
            "trading_status": book.trading_status.value,
            "raw_status": book.raw_status,
            "previous_close": book.previous_close,
            "cumulative_amount": book.cumulative_amount,
            "cumulative_volume": book.cumulative_volume,
            "market_phase": book.market_phase.value,
            "instrument_state": book.instrument_state.value,
            "state_confidence": book.state_confidence.value,
            "state_reasons": list(book.state_reasons),
            "quote_inactivity_age_ms": book.quote_inactivity_age_ms,
            "price_limit_source": book.price_limit_source,
            "upper_limit_price": book.upper_limit_price,
            "lower_limit_price": book.lower_limit_price,
            "sequence_number": book.sequence_number,
            "source": book.source,
        }
        return EventEnvelope(
            event_type=EventType.ORDER_BOOK_UPDATED,
            event_time=book.exchange_timestamp,
            received_time=received_time or book.receive_timestamp,
            source=book.source,
            instrument_id=instrument_id,
            trade_date=trade_date,
            sequence_number=book.sequence_number,
            correlation_id=correlation_id,
            payload=payload,
        )
