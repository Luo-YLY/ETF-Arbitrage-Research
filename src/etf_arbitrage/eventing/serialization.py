"""Canonical JSON serialization for domain events."""

from __future__ import annotations

from datetime import date, datetime
import json
from typing import Any, Mapping

from etf_arbitrage.domain import EventEnvelope, EventType, Exchange, InstrumentId
from etf_arbitrage.domain.events import canonical_json


def _datetime(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class EventSerializer:
    @staticmethod
    def to_dict(event: EventEnvelope) -> dict[str, Any]:
        return {
            "event_id": event.event_id,
            "event_type": event.event_type.value,
            "event_time": event.event_time.isoformat(),
            "received_time": event.received_time.isoformat(),
            "source": event.source,
            "exchange": event.exchange.value,
            "instrument_id": event.instrument_id.to_dict(),
            "trade_date": event.trade_date.isoformat(),
            "sequence_number": event.sequence_number,
            "correlation_id": event.correlation_id,
            "causation_id": event.causation_id,
            "schema_version": event.schema_version,
            "payload": json.loads(canonical_json(event.payload)),
            "payload_hash": event.payload_hash,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> EventEnvelope:
        return EventEnvelope(
            event_id=str(value.get("event_id", "")),
            event_type=EventType(str(value["event_type"])),
            event_time=_datetime(str(value["event_time"])),
            received_time=_datetime(str(value["received_time"])),
            source=str(value["source"]),
            exchange=Exchange(str(value["exchange"])),
            instrument_id=InstrumentId.from_dict(value["instrument_id"]),
            trade_date=date.fromisoformat(str(value["trade_date"])),
            sequence_number=(
                int(value["sequence_number"])
                if value.get("sequence_number") is not None
                else None
            ),
            correlation_id=(
                str(value["correlation_id"])
                if value.get("correlation_id") is not None
                else None
            ),
            causation_id=(
                str(value["causation_id"])
                if value.get("causation_id") is not None
                else None
            ),
            schema_version=int(value.get("schema_version", 1)),
            payload=value.get("payload", {}),
            payload_hash=str(value.get("payload_hash", "")),
        )

    @classmethod
    def dumps(cls, event: EventEnvelope) -> str:
        return json.dumps(
            cls.to_dict(event),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def loads(cls, value: str) -> EventEnvelope:
        payload = json.loads(value)
        if not isinstance(payload, dict):
            raise ValueError("Serialized event must be a JSON object")
        return cls.from_dict(payload)
