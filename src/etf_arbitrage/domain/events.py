"""Immutable, versioned domain-event envelopes."""

from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from enum import Enum
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Mapping, Optional

from .instruments import Exchange, InstrumentId


class EventType(str, Enum):
    ORDER_BOOK_UPDATED = "OrderBookUpdated"
    LAST_PRICE_UPDATED = "LastPriceUpdated"
    TRADING_STATUS_CHANGED = "TradingStatusChanged"
    OFFICIAL_IOPV_UPDATED = "OfficialIOPVUpdated"
    MARKET_DATA_SEQUENCE_GAP_DETECTED = "MarketDataSequenceGapDetected"
    MARKET_DATA_SOURCE_DISCONNECTED = "MarketDataSourceDisconnected"
    MARKET_DATA_SOURCE_RECONNECTED = "MarketDataSourceReconnected"

    PCF_PUBLISHED = "PCFPublished"
    PCF_VALIDATED = "PCFValidated"
    PCF_REJECTED = "PCFRejected"
    PCF_REVISED = "PCFRevised"
    PCF_COMPONENT_CHANGED = "PCFComponentChanged"
    PCF_SUBSTITUTION_RULE_CHANGED = "PCFSubstitutionRuleChanged"
    PCF_CREATION_REDEMPTION_STATUS_CHANGED = (
        "PCFCreationRedemptionStatusChanged"
    )
    PCF_LIMIT_CHANGED = "PCFLimitChanged"

    SESSION_PHASE_CHANGED = "SessionPhaseChanged"
    TRADING_DAY_STARTED = "TradingDayStarted"
    TRADING_DAY_ENDED = "TradingDayEnded"

    SNAPSHOT_ASSEMBLED = "SnapshotAssembled"
    SNAPSHOT_REJECTED = "SnapshotRejected"
    OPPORTUNITY_DETECTED = "OpportunityDetected"
    OPPORTUNITY_REJECTED = "OpportunityRejected"
    RISK_BLOCKED = "RiskBlocked"
    RISK_UNBLOCKED = "RiskUnblocked"

    PAPER_ORDER_CREATED = "PaperOrderCreated"
    PAPER_ORDER_FILLED = "PaperOrderFilled"
    CREATION_REDEMPTION_REQUESTED = "CreationRedemptionRequested"
    CREATION_REDEMPTION_CONFIRMED = "CreationRedemptionConfirmed"
    CREATION_REDEMPTION_REJECTED = "CreationRedemptionRejected"
    CASH_ADJUSTMENT_SETTLED = "CashAdjustmentSettled"


SOURCE_EVENT_TYPES = frozenset(
    {
        EventType.ORDER_BOOK_UPDATED,
        EventType.LAST_PRICE_UPDATED,
        EventType.TRADING_STATUS_CHANGED,
        EventType.OFFICIAL_IOPV_UPDATED,
        EventType.MARKET_DATA_SEQUENCE_GAP_DETECTED,
        EventType.MARKET_DATA_SOURCE_DISCONNECTED,
        EventType.MARKET_DATA_SOURCE_RECONNECTED,
        EventType.PCF_PUBLISHED,
        EventType.PCF_VALIDATED,
        EventType.PCF_REJECTED,
        EventType.PCF_REVISED,
        EventType.PCF_COMPONENT_CHANGED,
        EventType.PCF_SUBSTITUTION_RULE_CHANGED,
        EventType.PCF_CREATION_REDEMPTION_STATUS_CHANGED,
        EventType.PCF_LIMIT_CHANGED,
        EventType.SESSION_PHASE_CHANGED,
        EventType.TRADING_DAY_STARTED,
        EventType.TRADING_DAY_ENDED,
    }
)

DERIVED_EVENT_TYPES = frozenset(set(EventType) - set(SOURCE_EVENT_TYPES))

_REPLAY_PRIORITY = {
    EventType.TRADING_DAY_STARTED: 0,
    EventType.SESSION_PHASE_CHANGED: 1,
    EventType.PCF_PUBLISHED: 10,
    EventType.PCF_VALIDATED: 11,
    EventType.PCF_REVISED: 11,
    EventType.PCF_REJECTED: 11,
}


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, InstrumentId):
        return value.to_dict()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in sorted(value.items(), key=lambda x: str(x[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=lambda item: repr(item))
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Event payload cannot contain NaN or infinity")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("Unsupported event payload value: {}".format(type(value).__name__))


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@dataclass(frozen=True)
class EventEnvelope:
    event_type: EventType
    event_time: datetime
    received_time: datetime
    source: str
    instrument_id: InstrumentId
    trade_date: date
    payload: Mapping[str, Any]
    exchange: Exchange | str | None = None
    sequence_number: Optional[int] = None
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None
    schema_version: int = 1
    payload_hash: str = ""
    event_id: str = ""

    def __post_init__(self) -> None:
        event_type = (
            self.event_type
            if isinstance(self.event_type, EventType)
            else EventType(str(self.event_type))
        )
        instrument_id = (
            self.instrument_id
            if isinstance(self.instrument_id, InstrumentId)
            else InstrumentId.from_dict(self.instrument_id)
        )
        exchange = (
            instrument_id.exchange
            if self.exchange is None
            else self.exchange
            if isinstance(self.exchange, Exchange)
            else Exchange(str(self.exchange).upper())
        )
        if exchange != instrument_id.exchange:
            raise ValueError("Event exchange must match instrument exchange")
        if not isinstance(self.event_time, datetime) or not isinstance(
            self.received_time, datetime
        ):
            raise TypeError("event_time and received_time must be datetime values")
        if not isinstance(self.trade_date, date):
            raise TypeError("trade_date must be a date")
        if not str(self.source).strip():
            raise ValueError("Event source cannot be empty")
        if self.schema_version <= 0:
            raise ValueError("schema_version must be positive")
        if self.sequence_number is not None and self.sequence_number < 0:
            raise ValueError("sequence_number cannot be negative")

        normalized_payload = _jsonable(self.payload)
        payload_hash = hashlib.sha256(
            canonical_json(normalized_payload).encode("utf-8")
        ).hexdigest()
        identity = {
            "event_type": event_type.value,
            "event_time": self.event_time.isoformat(),
            "source": str(self.source).strip(),
            "instrument_id": instrument_id.key,
            "trade_date": self.trade_date.isoformat(),
            "sequence_number": self.sequence_number,
            "schema_version": self.schema_version,
            "payload_hash": payload_hash,
        }
        event_id = "evt-{}".format(
            hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()[:32]
        )
        if self.payload_hash and self.payload_hash != payload_hash:
            raise ValueError("payload_hash does not match payload")
        if self.event_id and self.event_id != event_id:
            raise ValueError("event_id does not match stable event identity")

        object.__setattr__(self, "event_type", event_type)
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "source", str(self.source).strip())
        object.__setattr__(self, "payload", _freeze(normalized_payload))
        object.__setattr__(self, "payload_hash", payload_hash)
        object.__setattr__(self, "event_id", event_id)


def event_sort_key(event: EventEnvelope) -> tuple:
    """Stable chronological ordering for deterministic replay."""

    sequence = event.sequence_number if event.sequence_number is not None else -1
    return (
        event.event_time,
        _REPLAY_PRIORITY.get(
            event.event_type,
            20 if event.event_type in SOURCE_EVENT_TYPES else 30,
        ),
        event.instrument_id.key,
        event.source,
        sequence,
        event.received_time,
        event.event_id,
    )
