"""Synchronous in-process event bus with duplicate suppression."""

from __future__ import annotations

from collections import defaultdict
from threading import RLock
from typing import Callable, DefaultDict, Optional, Protocol

from etf_arbitrage.domain import EventEnvelope, EventType


EventHandler = Callable[[EventEnvelope], None]


class EventPublisher(Protocol):
    def publish(self, event: EventEnvelope) -> bool:
        """Publish once; return False when the event is a duplicate."""


class EventSubscriber(Protocol):
    def __call__(self, event: EventEnvelope) -> None:
        """Handle one event."""


class EventBus(EventPublisher, Protocol):
    def subscribe(
        self,
        subscriber: EventHandler,
        event_type: Optional[EventType] = None,
    ) -> None:
        """Register a subscriber for one event type or all events."""

    def unsubscribe(
        self,
        subscriber: EventHandler,
        event_type: Optional[EventType] = None,
    ) -> None:
        """Remove a subscription."""


class InMemoryEventBus:
    """Deterministic, thread-safe event dispatch for the first local phase."""

    def __init__(self, suppress_duplicates: bool = True) -> None:
        self.suppress_duplicates = suppress_duplicates
        self._subscribers: DefaultDict[Optional[EventType], list[EventHandler]] = (
            defaultdict(list)
        )
        self._seen_event_ids: set[str] = set()
        self._lock = RLock()

    def subscribe(
        self,
        subscriber: EventHandler,
        event_type: Optional[EventType] = None,
    ) -> None:
        normalized = (
            event_type
            if event_type is None or isinstance(event_type, EventType)
            else EventType(str(event_type))
        )
        with self._lock:
            if subscriber not in self._subscribers[normalized]:
                self._subscribers[normalized].append(subscriber)

    def unsubscribe(
        self,
        subscriber: EventHandler,
        event_type: Optional[EventType] = None,
    ) -> None:
        normalized = (
            event_type
            if event_type is None or isinstance(event_type, EventType)
            else EventType(str(event_type))
        )
        with self._lock:
            if subscriber in self._subscribers[normalized]:
                self._subscribers[normalized].remove(subscriber)

    def publish(self, event: EventEnvelope) -> bool:
        with self._lock:
            if self.suppress_duplicates and event.event_id in self._seen_event_ids:
                return False
            self._seen_event_ids.add(event.event_id)
            subscribers = tuple(
                self._subscribers[None] + self._subscribers[event.event_type]
            )
        for subscriber in subscribers:
            subscriber(event)
        return True

    def reset_deduplication(self) -> None:
        with self._lock:
            self._seen_event_ids.clear()

    @property
    def seen_count(self) -> int:
        with self._lock:
            return len(self._seen_event_ids)
