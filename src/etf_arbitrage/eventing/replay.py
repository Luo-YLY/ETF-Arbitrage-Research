"""Deterministic event replay with optional atomic checkpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Optional

from etf_arbitrage.domain import EventType, Exchange, InstrumentId
from .bus import EventPublisher
from .store import EventStore


@dataclass(frozen=True)
class ReplayResult:
    read_count: int
    published_count: int
    duplicate_count: int
    last_event_id: Optional[str]
    last_event_time: Optional[datetime]


class EventReplay:
    def __init__(self, store: EventStore, publisher: EventPublisher) -> None:
        self.store = store
        self.publisher = publisher

    def replay(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        checkpoint_consumer: Optional[str] = None,
        event_types: Optional[Iterable[EventType]] = None,
        exchanges: Optional[Iterable[Exchange]] = None,
        instruments: Optional[Iterable[InstrumentId]] = None,
    ) -> ReplayResult:
        checkpoint = (
            self.store.load_checkpoint(checkpoint_consumer)
            if checkpoint_consumer
            else {}
        )
        last_checkpoint_id = checkpoint.get("last_event_id")
        skip_until_checkpoint = bool(last_checkpoint_id)
        read_count = published_count = duplicate_count = 0
        last_event_id = None
        last_event_time = None
        for event in self.store.iter_events(
            start=start,
            end=end,
            event_types=event_types,
            exchanges=exchanges,
            instruments=instruments,
        ):
            read_count += 1
            if skip_until_checkpoint:
                if event.event_id == last_checkpoint_id:
                    skip_until_checkpoint = False
                continue
            if self.publisher.publish(event):
                published_count += 1
            else:
                duplicate_count += 1
            last_event_id = event.event_id
            last_event_time = event.event_time
            if checkpoint_consumer:
                self.store.save_checkpoint(
                    checkpoint_consumer,
                    {
                        "last_event_id": event.event_id,
                        "last_event_time": event.event_time.isoformat(),
                    },
                )
        if skip_until_checkpoint:
            raise ValueError("Replay checkpoint event was not found in selected range")
        return ReplayResult(
            read_count=read_count,
            published_count=published_count,
            duplicate_count=duplicate_count,
            last_event_id=last_event_id,
            last_event_time=last_event_time,
        )
