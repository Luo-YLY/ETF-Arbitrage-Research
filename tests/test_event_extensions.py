from datetime import date, datetime, timedelta
from pathlib import Path
import shutil
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from etf_arbitrage.data import PCFComponent, PCFDocument, SubstituteFlag
from etf_arbitrage.domain import (
    DERIVED_EVENT_TYPES,
    SOURCE_EVENT_TYPES,
    EventEnvelope,
    EventType,
    Exchange,
    InstrumentId,
)
from etf_arbitrage.engine import PaperEngineResult
from etf_arbitrage.eventing import (
    EventDrivenOpportunityEngine,
    EventDrivenResearchPipeline,
    EventAuditIndex,
    EventReplay,
    FileEventStore,
    InMemoryEventBus,
    MarketSessionEventGenerator,
    PaperOutcomeProjector,
    SnapshotAssembler,
    SnapshotEventAdapter,
)
from etf_arbitrage.exchanges import default_exchange_registry
from etf_arbitrage.execution import (
    CycleState,
    PaperTradeCycle,
    SimulatedFill,
    SimulatedOrder,
)
from etf_arbitrage.market_data import (
    MarketSnapshot,
    OrderBook,
    OrderBookLevel,
    TradingStatus,
)
from etf_arbitrage.primary_market import (
    PrimaryMarketRequest,
    PrimaryMarketStatus,
)
from etf_arbitrage.strategies.event_driven import validated_pcf_event


TZ = ZoneInfo("Asia/Shanghai")
T0 = datetime(2026, 7, 22, 9, 30, tzinfo=TZ)


def component(
    code: str,
    *,
    security_id_source: str = "102",
    exchange: str = "SZSE",
) -> PCFComponent:
    return PCFComponent(
        stock_code=code,
        security_id_source=security_id_source,
        symbol=code,
        component_share=1000.0,
        substitute_flag=SubstituteFlag.PROHIBITED,
        premium_ratio=0.0,
        creation_cash_substitute=0.0,
        redemption_cash_substitute=0.0,
        exchange=exchange,
    )


def pcf(value: PCFComponent) -> PCFDocument:
    return PCFDocument(
        version="1.0",
        etf_code="159915",
        security_id_source="102",
        symbol="ETF",
        fund_management_company="test",
        underlying_index="399006",
        underlying_security_id_source="102",
        creation_redemption_unit=1000,
        estimate_cash_component=0.0,
        max_cash_ratio=0.0,
        publish=True,
        creation_allowed=True,
        redemption_allowed=True,
        record_num=1,
        total_record_num=1,
        trading_day=date(2026, 7, 22),
        previous_trading_day=date(2026, 7, 21),
        cash_component=0.0,
        nav_per_creation_unit=1000.0,
        nav=1.0,
        components=(value,),
        listing_exchange="SZSE",
        schema_version="1.0",
        parser_version="test",
        file_hash="event-extension-test",
    )


def book(
    instrument_id: InstrumentId,
    price: float,
    *,
    sequence: int,
) -> OrderBook:
    return OrderBook(
        symbol=instrument_id.code,
        exchange=instrument_id.exchange.value,
        exchange_timestamp=T0,
        receive_timestamp=T0 + timedelta(milliseconds=1),
        last_price=price,
        bids=(OrderBookLevel(price - 0.001, 10_000_000.0),),
        asks=(OrderBookLevel(price + 0.001, 10_000_000.0),),
        trading_status=TradingStatus.NORMAL,
        sequence_number=sequence,
        source="unit-test",
    )


def snapshot(document: PCFDocument) -> MarketSnapshot:
    component_id = document.components[0].instrument_id
    component_book = book(component_id, 1.0, sequence=1)
    return MarketSnapshot(
        snapshot_timestamp=T0,
        etf_order_book=book(document.etf_id, 1.2, sequence=2),
        component_order_books={
            document.components[0].stock_code: component_book
        },
        source_mode="UNIT_TEST",
    )


def test_registry_and_market_clock_expose_capabilities_and_transitions() -> None:
    registry = default_exchange_registry()
    matrix = registry.capability_matrix()

    assert matrix["SZSE"]["pcf_parser"]
    assert matrix["SSE"]["instrument_codec"]
    assert not matrix["SSE"]["production_ready"]

    clock = MarketSessionEventGenerator(registry)
    before_open = datetime(2026, 7, 22, 9, 0, tzinfo=TZ)
    first = clock.observe(Exchange.SZSE, before_open)
    assert [event.event_type for event in first] == [
        EventType.TRADING_DAY_STARTED,
        EventType.SESSION_PHASE_CHANGED,
    ]
    assert clock.observe(Exchange.SZSE, before_open) == ()

    opened = clock.observe(
        Exchange.SZSE,
        datetime(2026, 7, 22, 9, 30, tzinfo=TZ),
    )
    assert [event.event_type for event in opened] == [
        EventType.SESSION_PHASE_CHANGED
    ]
    ended = clock.observe(
        Exchange.SZSE,
        datetime(2026, 7, 22, 15, 1, tzinfo=TZ),
    )
    assert [event.event_type for event in ended] == [
        EventType.SESSION_PHASE_CHANGED,
        EventType.TRADING_DAY_ENDED,
    ]
    with pytest.raises(NotImplementedError, match="SSE exchange calendar"):
        clock.observe(Exchange.SSE, before_open)


def test_snapshot_adapter_and_assembler_keep_cross_market_component_id() -> None:
    document = pcf(
        component(
            "600000",
            security_id_source="101",
            exchange="SSE",
        )
    )
    value = snapshot(document)
    adapter = SnapshotEventAdapter.from_pcf(document)
    events = adapter.to_events(value, trade_date=document.trading_day)

    assert events[0].instrument_id == InstrumentId(Exchange.SSE, "600000")
    assembler = SnapshotAssembler()
    assembler.process(validated_pcf_event(document, T0, T0, source="unit-test"))
    results = ()
    for event in events:
        results = assembler.process(event)

    assert results[0].snapshot is not None
    assert set(results[0].snapshot.qualified_component_order_books) == {
        "SSE:600000"
    }
    assert set(results[0].snapshot.component_order_books) == {"600000"}


def test_pipeline_projects_opportunities_and_replays_source_events_only() -> None:
    root = Path("tmp") / "tests" / uuid4().hex
    document = pcf(component("000001"))
    bus = InMemoryEventBus()
    published: list[EventEnvelope] = []
    bus.subscribe(published.append)
    store = FileEventStore(root / "events")
    opportunity_engine = EventDrivenOpportunityEngine()
    pipeline = EventDrivenResearchPipeline(
        event_store=store,
        event_bus=bus,
        snapshot_assembler=SnapshotAssembler(),
        snapshot_projector=opportunity_engine.project,
    )

    try:
        pipeline.ingest(
            validated_pcf_event(document, T0, T0, source="unit-test")
        )
        results = pipeline.ingest_many(
            SnapshotEventAdapter.from_pcf(document).to_events(
                snapshot(document),
                trade_date=document.trading_day,
            )
        )
        outcome_types = {
            event.event_type
            for result in results
            for event in result.outcome_events
        }
        assert EventType.SNAPSHOT_ASSEMBLED in outcome_types
        assert {
            EventType.OPPORTUNITY_DETECTED,
            EventType.OPPORTUNITY_REJECTED,
        } & outcome_types
        opportunity_events = [
            event
            for event in store.iter_events(
                event_types={
                    EventType.OPPORTUNITY_DETECTED,
                    EventType.OPPORTUNITY_REJECTED,
                }
            )
            if event.payload.get("projection_scope") != "PAPER_EXECUTION"
        ]
        assert len(opportunity_events) == 2
        assert all(
            event.payload["live_executable"] is False
            for event in opportunity_events
        )

        stored_before = tuple(store.iter_events())
        original_derived_ids = {
            event.event_id
            for event in stored_before
            if event.event_type in DERIVED_EVENT_TYPES
        }
        published.clear()
        pipeline.reset_projection()
        replay_result = EventReplay(store, pipeline).replay(
            event_types=SOURCE_EVENT_TYPES
        )
        replayed_derived_ids = {
            event.event_id
            for event in published
            if event.event_type in DERIVED_EVENT_TYPES
        }

        assert replay_result.read_count == len(
            [
                event
                for event in stored_before
                if event.event_type in SOURCE_EVENT_TYPES
            ]
        )
        assert replayed_derived_ids == original_derived_ids
        assert tuple(store.iter_events()) == stored_before
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_paper_projector_emits_order_fill_primary_and_cash_events() -> None:
    instrument_id = InstrumentId(Exchange.SZSE, "159915")
    trigger = EventEnvelope(
        event_type=EventType.SNAPSHOT_ASSEMBLED,
        event_time=T0,
        received_time=T0,
        source="unit-test",
        instrument_id=instrument_id,
        trade_date=T0.date(),
        payload={"status": "EXECUTABLE"},
    )
    order = SimulatedOrder(
        order_id="order-1",
        cycle_id="cycle-1",
        symbol="159915",
        side="SELL",
        quantity=1000,
        order_type="MARKETABLE_LIMIT",
        submit_time=T0,
    )
    fill = SimulatedFill(
        fill_id="fill-1",
        order_id=order.order_id,
        cycle_id=order.cycle_id,
        symbol=order.symbol,
        side=order.side,
        quantity=order.quantity,
        average_price=1.2,
        total_value=1200.0,
        fill_time=T0 + timedelta(milliseconds=10),
        levels_consumed=1,
    )
    request = PrimaryMarketRequest(
        request_id="request-1",
        cycle_id=order.cycle_id,
        etf_code="159915",
        direction="CREATION",
        cu_count=1,
        submit_time=T0 + timedelta(milliseconds=20),
        confirm_time=T0 + timedelta(milliseconds=30),
        final_settlement_time=T0 + timedelta(milliseconds=40),
        pcf_date=T0.date(),
        estimated_cash_component=1.0,
        final_cash_component=1.1,
        status=PrimaryMarketStatus.FINAL_CASH_SETTLED,
        reject_reason=None,
        status_history=(
            PrimaryMarketStatus.REQUEST_SUBMITTED.value,
            PrimaryMarketStatus.REQUEST_CONFIRMED.value,
            PrimaryMarketStatus.FINAL_CASH_SETTLED.value,
        ),
    )
    cycle = PaperTradeCycle(
        cycle_id=order.cycle_id,
        direction="CREATION",
        decision_time=T0,
        fill_time=fill.fill_time,
        state=CycleState.CLOSED,
        state_history=(CycleState.CLOSED.value,),
        orders=(order,),
        fills=(fill,),
        primary_request_id=request.request_id,
        snapshot_profit=100.0,
        execution_profit=90.0,
        final_pnl=89.9,
        rejection_reason=None,
        execution_risk_label="PAPER_ONLY",
    )
    result = PaperEngineResult(
        decision_evaluation=None,  # type: ignore[arg-type]
        fill_evaluation=None,
        cycle=cycle,
        primary_request=request,
    )

    events = PaperOutcomeProjector().project(result, trigger)

    assert [event.event_type for event in events] == [
        EventType.PAPER_ORDER_CREATED,
        EventType.PAPER_ORDER_FILLED,
        EventType.CREATION_REDEMPTION_REQUESTED,
        EventType.CREATION_REDEMPTION_CONFIRMED,
        EventType.CASH_ADJUSTMENT_SETTLED,
    ]
    assert all(
        event.payload.get("paper_only", event.payload.get("emulated"))
        for event in events
    )

    index = EventAuditIndex((trigger, *events))
    chain = index.causal_chain(events[-1].event_id)
    assert [event.event_id for event in chain] == [
        trigger.event_id,
        events[-1].event_id,
    ]
    assert events[-1] in index.descendants(trigger.event_id)
    assert index.missing_causation_ids() == ()
