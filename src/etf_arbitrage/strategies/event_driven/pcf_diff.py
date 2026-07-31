"""Structured PCF changes emitted as research events.

PCF membership is evidence about the creation/redemption basket. It is not
treated as proof that the fund has completed the same portfolio trade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

from etf_arbitrage.data.pcf import PCFComponent, PCFDocument
from etf_arbitrage.domain import EventEnvelope, EventType, InstrumentId


@dataclass(frozen=True)
class PCFChange:
    event_type: EventType
    field: str
    before: Any
    after: Any
    component_id: Optional[InstrumentId] = None

    def to_payload(
        self,
        previous: PCFDocument,
        current: PCFDocument,
    ) -> dict[str, Any]:
        return {
            "field": self.field,
            "before": self.before,
            "after": self.after,
            "component_id": (
                self.component_id.to_dict() if self.component_id is not None else None
            ),
            "previous_pcf_version": previous.event_version,
            "current_pcf_version": current.event_version,
            "previous_trading_day": previous.trading_day.isoformat(),
            "current_trading_day": current.trading_day.isoformat(),
            "holdings_inference": "NOT_ESTABLISHED",
        }


@dataclass(frozen=True)
class PCFDiffResult:
    previous_version: str
    current_version: str
    changes: tuple[PCFChange, ...]

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    def to_events(
        self,
        previous: PCFDocument,
        current: PCFDocument,
        event_time: datetime,
        received_time: datetime,
        source: str = "pcf_diff",
    ) -> tuple[EventEnvelope, ...]:
        correlation_id = "pcf-diff:{}:{}:{}".format(
            current.etf_id.key,
            previous.event_version[:16],
            current.event_version[:16],
        )
        return tuple(
            EventEnvelope(
                event_type=change.event_type,
                event_time=event_time,
                received_time=received_time,
                source=source,
                instrument_id=current.etf_id,
                trade_date=current.trading_day,
                correlation_id=correlation_id,
                payload=change.to_payload(previous, current),
            )
            for change in self.changes
        )


def validated_pcf_event(
    pcf: PCFDocument,
    event_time: datetime,
    received_time: datetime,
    source: str = "pcf_validator",
    correlation_id: Optional[str] = None,
) -> EventEnvelope:
    return EventEnvelope(
        event_type=EventType.PCF_VALIDATED,
        event_time=event_time,
        received_time=received_time,
        source=source,
        instrument_id=pcf.etf_id,
        trade_date=pcf.trading_day,
        correlation_id=correlation_id,
        payload={
            "pcf": pcf.to_dict(),
            "validation": {
                "status": "VALID",
                "holdings_inference": "NOT_ESTABLISHED",
            },
        },
    )


def compare_pcf(previous: PCFDocument, current: PCFDocument) -> PCFDiffResult:
    if previous.etf_id != current.etf_id:
        raise ValueError("Cannot compare PCFs for different ETFs")
    changes: list[PCFChange] = []

    previous_components = {
        component.instrument_id: component for component in previous.components
    }
    current_components = {
        component.instrument_id: component for component in current.components
    }
    for instrument_id in sorted(
        previous_components.keys() - current_components.keys(),
        key=lambda item: item.key,
    ):
        changes.append(
            PCFChange(
                EventType.PCF_COMPONENT_CHANGED,
                "component_removed",
                previous_components[instrument_id].to_dict(),
                None,
                instrument_id,
            )
        )
    for instrument_id in sorted(
        current_components.keys() - previous_components.keys(),
        key=lambda item: item.key,
    ):
        changes.append(
            PCFChange(
                EventType.PCF_COMPONENT_CHANGED,
                "component_added",
                None,
                current_components[instrument_id].to_dict(),
                instrument_id,
            )
        )
    for instrument_id in sorted(
        previous_components.keys() & current_components.keys(),
        key=lambda item: item.key,
    ):
        _component_changes(
            previous_components[instrument_id],
            current_components[instrument_id],
            changes,
        )

    _document_change(
        changes,
        EventType.PCF_CREATION_REDEMPTION_STATUS_CHANGED,
        "creation_allowed",
        previous.creation_allowed,
        current.creation_allowed,
    )
    _document_change(
        changes,
        EventType.PCF_CREATION_REDEMPTION_STATUS_CHANGED,
        "redemption_allowed",
        previous.redemption_allowed,
        current.redemption_allowed,
    )
    for field in (
        "creation_limit",
        "redemption_limit",
        "net_creation_limit",
        "net_redemption_limit",
        "net_creation_limit_per_account",
        "net_redemption_limit_per_account",
        "total_creation_limit_per_account",
        "total_redemption_limit_per_account",
    ):
        _document_change(
            changes,
            EventType.PCF_LIMIT_CHANGED,
            field,
            getattr(previous, field),
            getattr(current, field),
        )
    for field in (
        "creation_redemption_unit",
        "creation_redemption_mechanism",
        "listing_exchange",
        "schema_version",
        "account_requirements",
        "settlement_rules",
    ):
        _document_change(
            changes,
            EventType.PCF_REVISED,
            field,
            getattr(previous, field),
            getattr(current, field),
        )

    return PCFDiffResult(
        previous_version=previous.event_version,
        current_version=current.event_version,
        changes=tuple(changes),
    )


def _component_changes(
    previous: PCFComponent,
    current: PCFComponent,
    changes: list[PCFChange],
) -> None:
    instrument_id = current.instrument_id
    _component_change(
        changes,
        EventType.PCF_COMPONENT_CHANGED,
        "component_share",
        previous.component_share,
        current.component_share,
        instrument_id,
    )
    for field in (
        "substitute_flag",
        "premium_ratio",
        "discount_ratio",
        "creation_cash_substitute",
        "redemption_cash_substitute",
    ):
        before = getattr(previous, field)
        after = getattr(current, field)
        if hasattr(before, "value"):
            before = before.value
        if hasattr(after, "value"):
            after = after.value
        _component_change(
            changes,
            EventType.PCF_SUBSTITUTION_RULE_CHANGED,
            field,
            before,
            after,
            instrument_id,
        )


def _document_change(
    changes: list[PCFChange],
    event_type: EventType,
    field: str,
    before: Any,
    after: Any,
) -> None:
    if before != after:
        changes.append(PCFChange(event_type, field, before, after))


def _component_change(
    changes: list[PCFChange],
    event_type: EventType,
    field: str,
    before: Any,
    after: Any,
    instrument_id: InstrumentId,
) -> None:
    if before != after:
        changes.append(
            PCFChange(event_type, field, before, after, instrument_id)
        )
