"""Read-only causal and correlation indexes for event-level problem tracing."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from etf_arbitrage.domain import EventEnvelope, event_sort_key


class EventAuditIndex:
    """Build a deterministic in-memory view over one selected event range."""

    def __init__(self, events: Iterable[EventEnvelope]) -> None:
        ordered = tuple(sorted(events, key=event_sort_key))
        self._events = ordered
        self._by_id = {event.event_id: event for event in ordered}
        self._by_correlation: dict[str, list[EventEnvelope]] = defaultdict(list)
        self._children: dict[str, list[EventEnvelope]] = defaultdict(list)
        for event in ordered:
            if event.correlation_id:
                self._by_correlation[event.correlation_id].append(event)
            if event.causation_id:
                self._children[event.causation_id].append(event)

    def get(self, event_id: str) -> EventEnvelope:
        try:
            return self._by_id[event_id]
        except KeyError as exc:
            raise KeyError("Unknown event_id {}".format(event_id)) from exc

    def causal_chain(self, event_id: str) -> tuple[EventEnvelope, ...]:
        """Return the available root-to-target causation chain."""

        chain: list[EventEnvelope] = []
        seen: set[str] = set()
        current = self.get(event_id)
        while True:
            if current.event_id in seen:
                raise ValueError(
                    "Causation cycle detected at {}".format(current.event_id)
                )
            seen.add(current.event_id)
            chain.append(current)
            if not current.causation_id:
                break
            parent = self._by_id.get(current.causation_id)
            if parent is None:
                break
            current = parent
        return tuple(reversed(chain))

    def correlated(self, correlation_id: str) -> tuple[EventEnvelope, ...]:
        return tuple(self._by_correlation.get(str(correlation_id), ()))

    def descendants(self, event_id: str) -> tuple[EventEnvelope, ...]:
        """Return all events causally downstream of one event."""

        self.get(event_id)
        found: list[EventEnvelope] = []
        queue = list(self._children.get(event_id, ()))
        seen: set[str] = set()
        while queue:
            event = queue.pop(0)
            if event.event_id in seen:
                continue
            seen.add(event.event_id)
            found.append(event)
            queue.extend(self._children.get(event.event_id, ()))
        return tuple(sorted(found, key=event_sort_key))

    def missing_causation_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    event.causation_id
                    for event in self._events
                    if event.causation_id
                    and event.causation_id not in self._by_id
                }
            )
        )
