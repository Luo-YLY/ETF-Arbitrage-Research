"""Adapters from existing normalized research models into source events."""

from __future__ import annotations

from datetime import date
from typing import Mapping, Optional

from etf_arbitrage.data.pcf import PCFDocument
from etf_arbitrage.domain import EventEnvelope, EventType, InstrumentId
from etf_arbitrage.market_data import MarketSnapshot, OrderBook

from .snapshot_assembler import order_book_to_payload


class SnapshotEventAdapter:
    """Explode a vendor-neutral snapshot into exchange-qualified source events."""

    def __init__(
        self,
        etf_id: InstrumentId,
        component_ids: Mapping[str, InstrumentId],
        *,
        source: str = "normalized_snapshot_adapter",
    ) -> None:
        self.etf_id = etf_id
        self.component_ids = {
            str(code): instrument_id
            for code, instrument_id in component_ids.items()
        }
        self.source = source

    @classmethod
    def from_pcf(
        cls,
        pcf: PCFDocument,
        *,
        source: str = "normalized_snapshot_adapter",
    ) -> "SnapshotEventAdapter":
        return cls(
            pcf.etf_id,
            {
                component.stock_code: component.instrument_id
                for component in pcf.components
            },
            source=source,
        )

    def to_events(
        self,
        snapshot: MarketSnapshot,
        *,
        trade_date: Optional[date] = None,
        correlation_id: Optional[str] = None,
    ) -> tuple[EventEnvelope, ...]:
        effective_date = trade_date or snapshot.snapshot_timestamp.date()
        correlation = correlation_id or snapshot.trigger_event_id
        identified_books = self._identified_component_books(snapshot)
        events = [
            self._book_event(
                instrument_id,
                book,
                effective_date,
                correlation,
            )
            for instrument_id, book in sorted(
                identified_books.items(), key=lambda item: item[0].key
            )
        ]
        events.append(
            self._book_event(
                self.etf_id,
                snapshot.etf_order_book,
                effective_date,
                correlation,
            )
        )
        if snapshot.official_iopv is not None:
            events.append(
                EventEnvelope(
                    event_type=EventType.OFFICIAL_IOPV_UPDATED,
                    event_time=snapshot.snapshot_timestamp,
                    received_time=snapshot.etf_order_book.receive_timestamp,
                    source=self.source,
                    instrument_id=self.etf_id,
                    trade_date=effective_date,
                    correlation_id=correlation,
                    payload={
                        "official_iopv": snapshot.official_iopv,
                        "source_mode": snapshot.source_mode,
                    },
                )
            )
        return tuple(events)

    def _identified_component_books(
        self,
        snapshot: MarketSnapshot,
    ) -> dict[InstrumentId, OrderBook]:
        identified: dict[InstrumentId, OrderBook] = {}
        for key, book in snapshot.qualified_component_order_books.items():
            identified[InstrumentId.parse(key)] = book

        for code, book in snapshot.component_order_books.items():
            instrument_id = self.component_ids.get(str(code))
            if instrument_id is None:
                raise KeyError(
                    "No exchange-qualified component ID for {}".format(code)
                )
            existing = identified.get(instrument_id)
            if existing is not None and existing != book:
                raise ValueError(
                    "Conflicting component books for {}".format(
                        instrument_id.key
                    )
                )
            identified[instrument_id] = book
        return identified

    def _book_event(
        self,
        instrument_id: InstrumentId,
        book: OrderBook,
        trade_date: date,
        correlation_id: Optional[str],
    ) -> EventEnvelope:
        return EventEnvelope(
            event_type=EventType.ORDER_BOOK_UPDATED,
            event_time=book.exchange_timestamp,
            received_time=book.receive_timestamp,
            source=book.source or self.source,
            instrument_id=instrument_id,
            trade_date=trade_date,
            sequence_number=book.sequence_number,
            correlation_id=correlation_id,
            payload=order_book_to_payload(book),
        )
