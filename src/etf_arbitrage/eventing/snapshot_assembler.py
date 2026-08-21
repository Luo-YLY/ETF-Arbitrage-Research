"""Build complete immutable pricing snapshots from normalized domain events."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Mapping, Optional

from etf_arbitrage.data.pcf import PCFDocument, SubstituteFlag
from etf_arbitrage.domain import EventEnvelope, EventType, InstrumentId
from etf_arbitrage.market_data.models import (
    DataQualityStatus,
    InstrumentState,
    MarketPhase,
    MarketSnapshot,
    OrderBook,
    OrderBookLevel,
    StateConfidence,
    TradingStatus,
)
from etf_arbitrage.market_data.state import MarketStateClassifier

from .dependency_graph import ETFDependencyGraph


class SnapshotAssemblyStatus(str, Enum):
    EXECUTABLE = "EXECUTABLE"
    INDICATIVE = "INDICATIVE"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class SnapshotAssemblyResult:
    etf_id: InstrumentId
    status: SnapshotAssemblyStatus
    snapshot: Optional[MarketSnapshot]
    blockers: tuple[str, ...]
    trigger_event_id: str
    trace_event_ids: tuple[str, ...]
    pcf_version: Optional[str]
    watermark: Optional[datetime]

    @property
    def executable(self) -> bool:
        return self.status == SnapshotAssemblyStatus.EXECUTABLE

    @property
    def indicative(self) -> bool:
        return self.status in {
            SnapshotAssemblyStatus.EXECUTABLE,
            SnapshotAssemblyStatus.INDICATIVE,
        }

    def to_event(self, trigger: EventEnvelope) -> EventEnvelope:
        event_type = (
            EventType.SNAPSHOT_REJECTED
            if self.status == SnapshotAssemblyStatus.REJECTED
            else EventType.SNAPSHOT_ASSEMBLED
        )
        return EventEnvelope(
            event_type=event_type,
            event_time=(
                self.snapshot.snapshot_timestamp
                if self.snapshot is not None
                else trigger.event_time
            ),
            received_time=trigger.received_time,
            source="snapshot_assembler",
            instrument_id=self.etf_id,
            trade_date=trigger.trade_date,
            correlation_id=trigger.correlation_id or trigger.event_id,
            causation_id=trigger.event_id,
            payload={
                "status": self.status.value,
                "executable": self.executable,
                "indicative": self.indicative,
                "blockers": list(self.blockers),
                "pcf_version": self.pcf_version,
                "watermark": self.watermark.isoformat() if self.watermark else None,
                "trace_event_ids": list(self.trace_event_ids),
            },
        )


@dataclass(frozen=True)
class _BookState:
    book: OrderBook
    event_id: str


@dataclass(frozen=True)
class _ValueState:
    value: float
    event_time: datetime
    event_id: str


def order_book_to_payload(book: OrderBook) -> dict[str, Any]:
    return {
        "symbol": book.symbol,
        "exchange": book.exchange,
        "exchange_timestamp": book.exchange_timestamp.isoformat(),
        "receive_timestamp": book.receive_timestamp.isoformat(),
        "last_price": book.last_price,
        "last_quantity": book.last_quantity,
        "bids": [
            {"price": level.price, "quantity": level.quantity} for level in book.bids
        ],
        "asks": [
            {"price": level.price, "quantity": level.quantity} for level in book.asks
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


def order_book_from_payload(
    payload: Mapping[str, Any],
    fallback_event: EventEnvelope,
) -> OrderBook:
    def levels(name: str) -> tuple[OrderBookLevel, ...]:
        return tuple(
            OrderBookLevel(float(item["price"]), float(item["quantity"]))
            for item in payload.get(name, ())
        )

    exchange_time = datetime.fromisoformat(
        str(
            payload.get(
                "exchange_timestamp", fallback_event.event_time.isoformat()
            )
        ).replace("Z", "+00:00")
    )
    receive_time = datetime.fromisoformat(
        str(
            payload.get(
                "receive_timestamp", fallback_event.received_time.isoformat()
            )
        ).replace("Z", "+00:00")
    )
    raw_status = payload.get("trading_status", TradingStatus.NORMAL.value)
    return OrderBook(
        symbol=str(payload.get("symbol", fallback_event.instrument_id.code)),
        exchange=str(
            payload.get("exchange", fallback_event.instrument_id.exchange.value)
        ),
        exchange_timestamp=exchange_time,
        receive_timestamp=receive_time,
        last_price=(
            float(payload["last_price"])
            if payload.get("last_price") is not None
            else None
        ),
        last_quantity=float(payload.get("last_quantity", 0.0)),
        bids=levels("bids"),
        asks=levels("asks"),
        trading_status=(
            raw_status
            if isinstance(raw_status, TradingStatus)
            else TradingStatus(str(raw_status))
        ),
        upper_limit_price=(
            float(payload["upper_limit_price"])
            if payload.get("upper_limit_price") is not None
            else None
        ),
        lower_limit_price=(
            float(payload["lower_limit_price"])
            if payload.get("lower_limit_price") is not None
            else None
        ),
        sequence_number=(
            int(payload["sequence_number"])
            if payload.get("sequence_number") is not None
            else fallback_event.sequence_number
        ),
        source=str(payload.get("source", fallback_event.source)),
        raw_status=str(payload.get("raw_status") or ""),
        previous_close=(
            float(payload["previous_close"])
            if payload.get("previous_close") is not None
            else None
        ),
        cumulative_amount=(
            float(payload["cumulative_amount"])
            if payload.get("cumulative_amount") is not None
            else None
        ),
        cumulative_volume=(
            float(payload["cumulative_volume"])
            if payload.get("cumulative_volume") is not None
            else None
        ),
        market_phase=MarketPhase(
            str(payload.get("market_phase") or MarketPhase.UNKNOWN.value)
        ),
        instrument_state=InstrumentState(
            str(payload.get("instrument_state") or InstrumentState.UNKNOWN.value)
        ),
        state_confidence=StateConfidence(
            str(payload.get("state_confidence") or StateConfidence.UNKNOWN.value)
        ),
        state_reasons=tuple(str(item) for item in payload.get("state_reasons", ())),
        quote_inactivity_age_ms=float(payload.get("quote_inactivity_age_ms", 0.0)),
        price_limit_source=str(payload.get("price_limit_source") or ""),
    )


class SnapshotAssembler:
    """Stateful event projection that never sends raw events to pricing engines."""

    def __init__(
        self,
        dependency_graph: Optional[ETFDependencyGraph] = None,
        allowed_lateness: timedelta = timedelta(0),
    ) -> None:
        if allowed_lateness < timedelta(0):
            raise ValueError("allowed_lateness cannot be negative")
        self.dependencies = dependency_graph or ETFDependencyGraph()
        self.allowed_lateness = allowed_lateness
        self._pcfs: dict[InstrumentId, PCFDocument] = {}
        self._pcf_event_ids: dict[InstrumentId, str] = {}
        self._books: dict[InstrumentId, _BookState] = {}
        self._official_iopv: dict[InstrumentId, _ValueState] = {}
        self._seen_event_ids: set[str] = set()
        self._last_sequences: dict[tuple[str, InstrumentId], int] = {}
        self._sequence_gaps: set[tuple[str, InstrumentId]] = set()
        self._disconnected_sources: set[str] = set()
        self._max_event_time: Optional[datetime] = None
        self.last_ignored_reason: Optional[str] = None
        self.late_event_count = 0
        self._state_classifier = MarketStateClassifier()

    @property
    def watermark(self) -> Optional[datetime]:
        if self._max_event_time is None:
            return None
        return self._max_event_time - self.allowed_lateness

    def process(self, event: EventEnvelope) -> tuple[SnapshotAssemblyResult, ...]:
        self.last_ignored_reason = None
        if event.event_id in self._seen_event_ids:
            self.last_ignored_reason = "DUPLICATE_EVENT"
            return ()
        self._seen_event_ids.add(event.event_id)
        if self._max_event_time is None or event.event_time > self._max_event_time:
            self._max_event_time = event.event_time

        if self._is_late_book_event(event):
            self.late_event_count += 1
            self.last_ignored_reason = "LATE_OR_OUT_OF_ORDER_EVENT"
            return ()

        self._track_sequence(event)
        affected = self._apply(event)
        return tuple(
            self._assemble(etf_id, event)
            for etf_id in sorted(affected, key=lambda item: item.key)
        )

    def reset(self) -> None:
        """Clear all projected state so a replay starts from a clean boundary."""

        self.dependencies.clear()
        self._pcfs.clear()
        self._pcf_event_ids.clear()
        self._books.clear()
        self._official_iopv.clear()
        self._seen_event_ids.clear()
        self._last_sequences.clear()
        self._sequence_gaps.clear()
        self._disconnected_sources.clear()
        self._max_event_time = None
        self.last_ignored_reason = None
        self.late_event_count = 0
        self._state_classifier.reset()

    def register_pcf(
        self,
        pcf: PCFDocument,
        event_id: str = "direct-registration",
    ) -> None:
        etf_id = pcf.etf_id
        self._pcfs[etf_id] = pcf
        self._pcf_event_ids[etf_id] = event_id
        self.dependencies.replace_etf(
            etf_id,
            (
                component.instrument_id
                for component in pcf.components
                if component.component_share > 0
                and not component.substitute_flag.requires_cash_substitution
            ),
            pcf.event_version,
        )

    def _is_late_book_event(self, event: EventEnvelope) -> bool:
        if event.event_type not in {
            EventType.ORDER_BOOK_UPDATED,
            EventType.LAST_PRICE_UPDATED,
            EventType.TRADING_STATUS_CHANGED,
        }:
            return False
        current = self._books.get(event.instrument_id)
        if current is None:
            return False
        return event.event_time < current.book.exchange_timestamp

    def _track_sequence(self, event: EventEnvelope) -> None:
        if event.sequence_number is None:
            return
        key = (event.source, event.instrument_id)
        previous = self._last_sequences.get(key)
        if previous is not None and event.sequence_number > previous + 1:
            self._sequence_gaps.add(key)
        if previous is None or event.sequence_number > previous:
            self._last_sequences[key] = event.sequence_number

    def _apply(self, event: EventEnvelope) -> frozenset[InstrumentId]:
        if event.event_type in {EventType.PCF_VALIDATED, EventType.PCF_REVISED}:
            pcf_payload = event.payload.get("pcf", event.payload)
            if not isinstance(pcf_payload, Mapping):
                raise ValueError("PCF event payload must contain a PCF mapping")
            pcf = PCFDocument.from_dict(pcf_payload)
            if pcf.etf_id != event.instrument_id:
                raise ValueError("PCF event instrument does not match PCF ETF")
            self.register_pcf(pcf, event.event_id)
            return frozenset({pcf.etf_id})

        if event.event_type == EventType.ORDER_BOOK_UPDATED:
            self._books[event.instrument_id] = _BookState(
                order_book_from_payload(event.payload, event),
                event.event_id,
            )
        elif event.event_type == EventType.LAST_PRICE_UPDATED:
            existing = self._books.get(event.instrument_id)
            if existing is None:
                book = order_book_from_payload(event.payload, event)
            else:
                book = replace(
                    existing.book,
                    exchange_timestamp=event.event_time,
                    receive_timestamp=event.received_time,
                    last_price=(
                        float(event.payload["last_price"])
                        if event.payload.get("last_price") is not None
                        else None
                    ),
                    last_quantity=float(event.payload.get("last_quantity", 0.0)),
                    sequence_number=event.sequence_number,
                    source=event.source,
                )
            self._books[event.instrument_id] = _BookState(book, event.event_id)
        elif event.event_type == EventType.TRADING_STATUS_CHANGED:
            existing = self._books.get(event.instrument_id)
            if existing is not None:
                status = TradingStatus(str(event.payload["trading_status"]))
                self._books[event.instrument_id] = _BookState(
                    replace(
                        existing.book,
                        exchange_timestamp=event.event_time,
                        receive_timestamp=event.received_time,
                        trading_status=status,
                        sequence_number=event.sequence_number,
                    ),
                    event.event_id,
                )
        elif event.event_type == EventType.OFFICIAL_IOPV_UPDATED:
            value = float(event.payload["official_iopv"])
            if value <= 0:
                raise ValueError("official_iopv must be positive")
            self._official_iopv[event.instrument_id] = _ValueState(
                value, event.event_time, event.event_id
            )
        elif event.event_type == EventType.MARKET_DATA_SEQUENCE_GAP_DETECTED:
            self._sequence_gaps.add((event.source, event.instrument_id))
        elif event.event_type == EventType.MARKET_DATA_SOURCE_DISCONNECTED:
            self._disconnected_sources.add(event.source)
            return frozenset(self._pcfs)
        elif event.event_type == EventType.MARKET_DATA_SOURCE_RECONNECTED:
            self._disconnected_sources.discard(event.source)
            self._sequence_gaps = {
                item for item in self._sequence_gaps if item[0] != event.source
            }
            return frozenset(self._pcfs)

        return self.dependencies.affected_etfs(event.instrument_id)

    def _assemble(
        self,
        etf_id: InstrumentId,
        trigger: EventEnvelope,
    ) -> SnapshotAssemblyResult:
        blockers: list[str] = []
        trace_ids = [trigger.event_id]
        pcf = self._pcfs.get(etf_id)
        if pcf is None:
            return self._rejected(
                etf_id, trigger, ("MISSING_VALIDATED_PCF",), trace_ids, None
            )
        pcf_event_id = self._pcf_event_ids.get(etf_id)
        if pcf_event_id:
            trace_ids.append(pcf_event_id)
        if pcf.trading_day != trigger.trade_date:
            blockers.append("PCF_DATE_MISMATCH")

        etf_state = self._books.get(etf_id)
        if etf_state is None:
            return self._rejected(
                etf_id,
                trigger,
                tuple(blockers + ["MISSING_ETF_BOOK"]),
                trace_ids,
                pcf,
            )
        if etf_state.book.exchange_timestamp > trigger.event_time:
            return self._rejected(
                etf_id,
                trigger,
                tuple(blockers + ["FUTURE_ETF_QUOTE"]),
                trace_ids,
                pcf,
            )
        etf_book = etf_state.book
        trace_ids.append(etf_state.event_id)

        component_books: dict[str, OrderBook] = {}
        qualified_component_books: dict[str, OrderBook] = {}
        legacy_ids: dict[str, InstrumentId] = {}
        required_components = [
            component
            for component in pcf.components
            if component.component_share > 0
            and not component.substitute_flag.requires_cash_substitution
        ]
        for component in required_components:
            instrument_id = component.instrument_id
            previous_id = legacy_ids.get(component.stock_code)
            if previous_id is not None and previous_id != instrument_id:
                blockers.append("AMBIGUOUS_COMPONENT_CODE:{}".format(component.stock_code))
                continue
            legacy_ids[component.stock_code] = instrument_id
            state = self._books.get(instrument_id)
            if state is None:
                blockers.append("MISSING_COMPONENT_BOOK:{}".format(instrument_id.key))
                continue
            if state.book.exchange_timestamp > trigger.event_time:
                blockers.append("FUTURE_COMPONENT_QUOTE:{}".format(instrument_id.key))
                continue
            component_books[component.stock_code] = state.book
            qualified_component_books[instrument_id.key] = state.book
            trace_ids.append(state.event_id)

        relevant_ids = {etf_id, *(component.instrument_id for component in required_components)}
        if any(key[1] in relevant_ids for key in self._sequence_gaps):
            blockers.append("SEQUENCE_GAP")
        if etf_book.source in self._disconnected_sources or any(
            book.source in self._disconnected_sources
            for book in component_books.values()
        ):
            blockers.append("SOURCE_DISCONNECTED")

        if not etf_book.has_two_sided_book:
            blockers.append("MISSING_ETF_TWO_SIDED_BOOK")
        for component in required_components:
            book = component_books.get(component.stock_code)
            if book is not None and not book.has_two_sided_book:
                blockers.append(
                    "MISSING_COMPONENT_TWO_SIDED_BOOK:{}".format(
                        component.instrument_id.key
                    )
                )

        official_iopv = None
        official_state = self._official_iopv.get(etf_id)
        if (
            official_state is not None
            and official_state.event_time <= trigger.event_time
        ):
            official_iopv = official_state.value
            trace_ids.append(official_state.event_id)

        unique_blockers = tuple(dict.fromkeys(blockers))
        invalid = any(
            blocker.startswith(
                (
                    "PCF_DATE_MISMATCH",
                    "FUTURE_",
                    "AMBIGUOUS_",
                    "SEQUENCE_GAP",
                    "SOURCE_DISCONNECTED",
                )
            )
            for blocker in unique_blockers
        )
        indicative = bool(
            etf_book.mid_price is not None
            and etf_book.mid_price > 0
            and len(component_books) == len(required_components)
            and all(
                book.mid_price is not None and book.mid_price > 0
                for book in component_books.values()
            )
            and not invalid
        )
        executable = indicative and not unique_blockers
        if not indicative:
            status = SnapshotAssemblyStatus.REJECTED
        elif executable:
            status = SnapshotAssemblyStatus.EXECUTABLE
        else:
            status = SnapshotAssemblyStatus.INDICATIVE

        quality = (
            DataQualityStatus.GOOD
            if executable
            else DataQualityStatus.DEGRADED
            if indicative
            else DataQualityStatus.INVALID
        )
        trace = tuple(dict.fromkeys(trace_ids))
        snapshot = (
            MarketSnapshot(
                snapshot_timestamp=trigger.event_time,
                etf_order_book=etf_book,
                component_order_books=component_books,
                qualified_component_order_books=qualified_component_books,
                official_iopv=official_iopv,
                data_quality_status=quality,
                source_mode=(
                    "EVENT_DRIVEN_EXECUTABLE"
                    if executable
                    else "EVENT_DRIVEN_INDICATIVE"
                    if indicative
                    else "EVENT_DRIVEN_REJECTED"
                ),
                sequence_gap="SEQUENCE_GAP" in unique_blockers,
                etf_instrument_id=etf_id.key,
                pcf_version=pcf.event_version,
                pcf_hash=pcf.file_hash or None,
                trigger_event_id=trigger.event_id,
                event_watermark=self.watermark,
                trace_event_ids=trace,
                assembly_blockers=unique_blockers,
            )
            if indicative
            else None
        )
        if snapshot is not None:
            snapshot = self._state_classifier.classify_snapshot(snapshot)
        return SnapshotAssemblyResult(
            etf_id=etf_id,
            status=status,
            snapshot=snapshot,
            blockers=unique_blockers,
            trigger_event_id=trigger.event_id,
            trace_event_ids=trace,
            pcf_version=pcf.event_version,
            watermark=self.watermark,
        )

    def _rejected(
        self,
        etf_id: InstrumentId,
        trigger: EventEnvelope,
        blockers: tuple[str, ...],
        trace_ids: list[str],
        pcf: Optional[PCFDocument],
    ) -> SnapshotAssemblyResult:
        return SnapshotAssemblyResult(
            etf_id=etf_id,
            status=SnapshotAssemblyStatus.REJECTED,
            snapshot=None,
            blockers=tuple(dict.fromkeys(blockers)),
            trigger_event_id=trigger.event_id,
            trace_event_ids=tuple(dict.fromkeys(trace_ids)),
            pcf_version=pcf.event_version if pcf is not None else None,
            watermark=self.watermark,
        )
