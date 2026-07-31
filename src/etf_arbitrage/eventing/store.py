"""Partitioned append-only event store and atomic consumer checkpoints."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime
import json
import os
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping, Optional

from etf_arbitrage.domain import (
    EventEnvelope,
    EventType,
    Exchange,
    InstrumentId,
    event_sort_key,
)

from .serialization import EventSerializer


class EventStore(ABC):
    @abstractmethod
    def append(self, event: EventEnvelope) -> bool:
        raise NotImplementedError

    @abstractmethod
    def iter_events(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        event_types: Optional[Iterable[EventType]] = None,
        exchanges: Optional[Iterable[Exchange]] = None,
        instruments: Optional[Iterable[InstrumentId]] = None,
    ) -> Iterable[EventEnvelope]:
        raise NotImplementedError

    @abstractmethod
    def save_checkpoint(self, consumer: str, value: Mapping[str, Any]) -> Path:
        raise NotImplementedError

    @abstractmethod
    def load_checkpoint(self, consumer: str) -> dict[str, Any]:
        raise NotImplementedError


class FileEventStore(EventStore):
    """JSONL event store under ``YYYYMMDD/exchange/event_type.jsonl``."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self._known_ids: dict[Path, set[str]] = {}
        self._lock = RLock()

    def partition_path(self, event: EventEnvelope) -> Path:
        return (
            self.root
            / event.trade_date.strftime("%Y%m%d")
            / event.exchange.value
            / "{}.jsonl".format(event.event_type.value)
        )

    def append(self, event: EventEnvelope) -> bool:
        path = self.partition_path(event)
        line = EventSerializer.dumps(event)
        with self._lock:
            ids = self._ids_for(path)
            if event.event_id in ids:
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            ids.add(event.event_id)
        return True

    def iter_events(
        self,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        event_types: Optional[Iterable[EventType]] = None,
        exchanges: Optional[Iterable[Exchange]] = None,
        instruments: Optional[Iterable[InstrumentId]] = None,
    ) -> Iterable[EventEnvelope]:
        type_filter = (
            {item if isinstance(item, EventType) else EventType(str(item)) for item in event_types}
            if event_types is not None
            else None
        )
        exchange_filter = (
            {
                item if isinstance(item, Exchange) else Exchange(str(item).upper())
                for item in exchanges
            }
            if exchanges is not None
            else None
        )
        instrument_filter = set(instruments) if instruments is not None else None
        events: list[EventEnvelope] = []
        if not self.root.exists():
            return iter(())
        for path in sorted(self.root.glob("*/*/*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                event = EventSerializer.loads(line)
                if start is not None and event.event_time < start:
                    continue
                if end is not None and event.event_time > end:
                    continue
                if type_filter is not None and event.event_type not in type_filter:
                    continue
                if exchange_filter is not None and event.exchange not in exchange_filter:
                    continue
                if (
                    instrument_filter is not None
                    and event.instrument_id not in instrument_filter
                ):
                    continue
                events.append(event)
        return iter(sorted(events, key=event_sort_key))

    def save_checkpoint(self, consumer: str, value: Mapping[str, Any]) -> Path:
        path = self._checkpoint_path(consumer)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    def load_checkpoint(self, consumer: str) -> dict[str, Any]:
        path = self._checkpoint_path(consumer)
        if not path.exists():
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Event checkpoint must be a JSON object")
        return payload

    def _ids_for(self, path: Path) -> set[str]:
        cached = self._known_ids.get(path)
        if cached is not None:
            return cached
        identifiers: set[str] = set()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    identifiers.add(EventSerializer.loads(line).event_id)
        self._known_ids[path] = identifiers
        return identifiers

    def _checkpoint_path(self, consumer: str) -> Path:
        normalized = "".join(
            character if character.isalnum() or character in {"-", "_"} else "_"
            for character in str(consumer)
        ).strip("_")
        if not normalized:
            raise ValueError("Checkpoint consumer cannot be empty")
        return self.root / "_checkpoints" / "{}.json".format(normalized)
