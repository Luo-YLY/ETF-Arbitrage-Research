from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
import shutil
from uuid import uuid4
from zoneinfo import ZoneInfo

from etf_arbitrage.data import PCFComponent, PCFDocument, SubstituteFlag
from etf_arbitrage.domain import EventEnvelope, EventType, Exchange, InstrumentId
from etf_arbitrage.eventing import (
    ETFDependencyGraph,
    EventDrivenResearchPipeline,
    FileEventStore,
    InMemoryEventBus,
    SnapshotAssembler,
    SnapshotAssemblyStatus,
    order_book_to_payload,
)
from etf_arbitrage.market_data import (
    OrderBook,
    OrderBookLevel,
    TradingStatus,
)
from etf_arbitrage.strategies.event_driven import validated_pcf_event


TZ = ZoneInfo("Asia/Shanghai")
T0 = datetime(2026, 7, 22, 9, 30, tzinfo=TZ)


def component(code: str) -> PCFComponent:
    return PCFComponent(
        stock_code=code,
        security_id_source="102",
        symbol=code,
        component_share=100.0,
        substitute_flag=SubstituteFlag.PROHIBITED,
        premium_ratio=0.0,
        creation_cash_substitute=0.0,
        redemption_cash_substitute=0.0,
        exchange="SZSE",
    )


def pcf(*codes: str, file_hash: str = "pcf-a") -> PCFDocument:
    components = tuple(component(code) for code in codes)
    return PCFDocument(
        version="1.0",
        etf_code="159915",
        security_id_source="102",
        symbol="创业板ETF",
        fund_management_company="test",
        underlying_index="399006",
        underlying_security_id_source="102",
        creation_redemption_unit=1000,
        estimate_cash_component=0.0,
        max_cash_ratio=0.0,
        publish=True,
        creation_allowed=True,
        redemption_allowed=True,
        record_num=len(components),
        total_record_num=len(components),
        trading_day=date(2026, 7, 22),
        previous_trading_day=date(2026, 7, 21),
        cash_component=0.0,
        nav_per_creation_unit=1000.0,
        nav=1.0,
        components=components,
        listing_exchange="SZSE",
        schema_version="1.0",
        parser_version="test",
        file_hash=file_hash,
    )


def book(
    instrument_id: InstrumentId,
    timestamp: datetime,
    *,
    two_sided: bool = True,
    last_price: float = 1.0,
    sequence: int = 1,
) -> OrderBook:
    return OrderBook(
        symbol=instrument_id.code,
        exchange=instrument_id.exchange.value,
        exchange_timestamp=timestamp,
        receive_timestamp=timestamp,
        last_price=last_price,
        bids=(OrderBookLevel(last_price - 0.001, 1_000_000.0),)
        if two_sided
        else (),
        asks=(OrderBookLevel(last_price + 0.001, 1_000_000.0),)
        if two_sided
        else (),
        trading_status=TradingStatus.NORMAL,
        sequence_number=sequence,
        source="unit-test",
    )


def book_event(
    instrument_id: InstrumentId,
    timestamp: datetime,
    *,
    two_sided: bool = True,
    last_price: float = 1.0,
    sequence: int = 1,
) -> EventEnvelope:
    value = book(
        instrument_id,
        timestamp,
        two_sided=two_sided,
        last_price=last_price,
        sequence=sequence,
    )
    return EventEnvelope(
        event_type=EventType.ORDER_BOOK_UPDATED,
        event_time=timestamp,
        received_time=timestamp,
        source="unit-test",
        instrument_id=instrument_id,
        trade_date=date(2026, 7, 22),
        sequence_number=sequence,
        payload=order_book_to_payload(value),
    )


def load_pcf(assembler: SnapshotAssembler, value: PCFDocument) -> None:
    assembler.process(validated_pcf_event(value, T0, T0, source="unit-test"))


def test_snapshot_assembler_builds_executable_snapshot() -> None:
    assembler = SnapshotAssembler()
    document = pcf("000001", "000002")
    load_pcf(assembler, document)
    for code in ("000001", "000002"):
        assembler.process(
            book_event(InstrumentId(Exchange.SZSE, code), T0 + timedelta(seconds=1))
        )

    results = assembler.process(
        book_event(document.etf_id, T0 + timedelta(seconds=2), last_price=1.01)
    )

    assert len(results) == 1
    result = results[0]
    assert result.status == SnapshotAssemblyStatus.EXECUTABLE
    assert result.snapshot is not None
    assert result.snapshot.etf_instrument_id == "SZSE:159915"
    assert set(result.snapshot.component_order_books) == {"000001", "000002"}
    assert result.snapshot.trigger_event_id
    assert result.snapshot.assembly_blockers == ()


def test_missing_bid_ask_produces_only_indicative_snapshot() -> None:
    assembler = SnapshotAssembler()
    document = pcf("000001")
    load_pcf(assembler, document)
    assembler.process(
        book_event(InstrumentId(Exchange.SZSE, "000001"), T0 + timedelta(seconds=1))
    )

    result = assembler.process(
        book_event(
            document.etf_id,
            T0 + timedelta(seconds=2),
            two_sided=False,
        )
    )[0]

    assert result.status == SnapshotAssemblyStatus.INDICATIVE
    assert not result.executable
    assert "MISSING_ETF_TWO_SIDED_BOOK" in result.blockers


def test_late_event_is_ignored_and_does_not_replace_newer_book() -> None:
    assembler = SnapshotAssembler()
    document = pcf("000001")
    load_pcf(assembler, document)
    component_id = InstrumentId(Exchange.SZSE, "000001")
    assembler.process(book_event(component_id, T0 + timedelta(seconds=2), last_price=2.0))

    assert not assembler.process(
        book_event(component_id, T0 + timedelta(seconds=1), last_price=1.0)
    )
    assert assembler.last_ignored_reason == "LATE_OR_OUT_OF_ORDER_EVENT"
    assert assembler.late_event_count == 1


def test_snapshot_never_uses_future_quote() -> None:
    assembler = SnapshotAssembler()
    document = pcf("000001")
    load_pcf(assembler, document)
    assembler.process(
        book_event(document.etf_id, T0 + timedelta(seconds=3), last_price=1.01)
    )

    result = assembler.process(
        book_event(
            InstrumentId(Exchange.SZSE, "000001"),
            T0 + timedelta(seconds=2),
        )
    )[0]

    assert result.status == SnapshotAssemblyStatus.REJECTED
    assert "FUTURE_ETF_QUOTE" in result.blockers


def test_sequence_gap_blocks_snapshot_until_source_reconnects() -> None:
    assembler = SnapshotAssembler()
    document = pcf("000001")
    load_pcf(assembler, document)
    component_id = InstrumentId(Exchange.SZSE, "000001")
    assembler.process(book_event(component_id, T0 + timedelta(seconds=1), sequence=1))
    assembler.process(book_event(component_id, T0 + timedelta(seconds=2), sequence=3))

    result = assembler.process(
        book_event(document.etf_id, T0 + timedelta(seconds=3))
    )[0]

    assert result.status == SnapshotAssemblyStatus.REJECTED
    assert "SEQUENCE_GAP" in result.blockers


def test_dependency_graph_switches_pcf_components_atomically() -> None:
    graph = ETFDependencyGraph()
    assembler = SnapshotAssembler(graph)
    first = pcf("000001", file_hash="pcf-a")
    second = replace(
        pcf("000002", file_hash="pcf-b"),
        version="1.1",
    )
    load_pcf(assembler, first)
    old_component = InstrumentId(Exchange.SZSE, "000001")
    new_component = InstrumentId(Exchange.SZSE, "000002")

    assembler.process(
        validated_pcf_event(
            second,
            T0 + timedelta(seconds=1),
            T0 + timedelta(seconds=1),
            source="unit-test",
        )
    )

    assert graph.affected_etfs(old_component) == frozenset()
    assert graph.affected_etfs(new_component) == frozenset({second.etf_id})
    assert graph.pcf_version(second.etf_id) == second.event_version


def test_pipeline_persists_source_and_outcome_events_once() -> None:
    root = Path(__file__).resolve().parents[1] / "tmp" / "tests" / uuid4().hex
    try:
        bus = InMemoryEventBus()
        published: list[EventEnvelope] = []
        bus.subscribe(published.append)
        store = FileEventStore(root)
        pipeline = EventDrivenResearchPipeline(
            event_store=store,
            event_bus=bus,
            snapshot_assembler=SnapshotAssembler(),
        )
        document = pcf("000001")
        pcf_event = validated_pcf_event(document, T0, T0, source="unit-test")

        first = pipeline.ingest(pcf_event)
        duplicate = pipeline.ingest(pcf_event)

        assert first.stored
        assert first.published
        assert len(first.outcome_events) == 1
        assert first.outcome_events[0].event_type == EventType.SNAPSHOT_REJECTED
        assert not duplicate.stored
        assert not duplicate.published
        assert len(tuple(store.iter_events())) == 2
        assert len(published) == 2
    finally:
        shutil.rmtree(root, ignore_errors=True)
