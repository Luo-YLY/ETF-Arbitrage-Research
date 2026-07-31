from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import shutil
from uuid import uuid4

from etf_arbitrage.domain import EventEnvelope, EventType, Exchange, InstrumentId
from etf_arbitrage.eventing import (
    EventReplay,
    EventSerializer,
    FileEventStore,
    InMemoryEventBus,
)


BASE_TIME = datetime(2026, 7, 22, 9, 30, tzinfo=timezone.utc)


def make_event(
    *,
    event_time=BASE_TIME,
    received_time=BASE_TIME,
    sequence=1,
    price=3.6,
):
    return EventEnvelope(
        event_type=EventType.LAST_PRICE_UPDATED,
        event_time=event_time,
        received_time=received_time,
        source="unit-test",
        instrument_id=InstrumentId(Exchange.SZSE, "159915"),
        trade_date=date(2026, 7, 22),
        sequence_number=sequence,
        payload={"last_price": price},
    )


def test_instrument_id_is_exchange_qualified() -> None:
    szse = InstrumentId.parse("159915", Exchange.SZSE)
    sse = InstrumentId.parse("SSE:510300")

    assert szse.key == "SZSE:159915"
    assert sse.key == "SSE:510300"
    assert szse != InstrumentId(Exchange.SSE, "159915")


def test_event_identity_is_stable_across_receive_times() -> None:
    first = make_event(received_time=BASE_TIME)
    duplicate = make_event(received_time=BASE_TIME + timedelta(milliseconds=8))

    assert first.event_id == duplicate.event_id
    assert first.payload_hash == duplicate.payload_hash


def test_event_serializer_round_trip_preserves_identity() -> None:
    event = make_event()

    restored = EventSerializer.loads(EventSerializer.dumps(event))

    assert restored == event
    assert restored.instrument_id == InstrumentId(Exchange.SZSE, "159915")


def test_in_memory_bus_suppresses_duplicate_events() -> None:
    bus = InMemoryEventBus()
    received = []
    bus.subscribe(received.append, EventType.LAST_PRICE_UPDATED)
    event = make_event()

    assert bus.publish(event)
    assert not bus.publish(event)
    assert received == [event]


def test_file_store_partitions_deduplicates_and_replays_without_future() -> None:
    root = Path("tmp") / "tests" / uuid4().hex
    store = FileEventStore(root / "events")
    first = make_event(sequence=1, price=3.6)
    future = make_event(
        event_time=BASE_TIME + timedelta(seconds=1),
        received_time=BASE_TIME + timedelta(seconds=1),
        sequence=2,
        price=3.61,
    )

    try:
        assert store.append(future)
        assert store.append(first)
        assert not store.append(first)
        assert store.partition_path(first).exists()

        selected = list(store.iter_events(end=BASE_TIME))
        assert selected == [first]

        bus = InMemoryEventBus()
        replayed = []
        bus.subscribe(replayed.append)
        result = EventReplay(store, bus).replay(end=BASE_TIME)

        assert result.published_count == 1
        assert replayed == [first]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_event_checkpoint_is_atomic_and_round_trips() -> None:
    root = Path("tmp") / "tests" / uuid4().hex
    store = FileEventStore(root / "events")
    try:
        path = store.save_checkpoint(
            "snapshot assembler", {"last_event_id": "evt-1"}
        )

        assert path.name == "snapshot_assembler.json"
        assert store.load_checkpoint("snapshot assembler") == {
            "last_event_id": "evt-1"
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)
