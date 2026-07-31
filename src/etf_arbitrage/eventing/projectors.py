"""Translate research and paper-engine results into immutable audit events."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
import math
from typing import Optional

from etf_arbitrage.arbitrage import ArbitrageEvaluation, CapacityResult
from etf_arbitrage.domain import EventEnvelope, EventType, InstrumentId
from etf_arbitrage.engine.paper_arbitrage_engine import PaperEngineResult
from etf_arbitrage.primary_market import PrimaryMarketStatus


def _finite_or_none(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _capacity_payload(
    capacity: Optional[CapacityResult],
) -> Optional[dict[str, object]]:
    if capacity is None:
        return None
    return {
        "max_executable_cu": capacity.max_executable_cu,
        "bottleneck_symbol": capacity.bottleneck_symbol,
        "bottleneck_direction": capacity.bottleneck_direction,
        "bottleneck_quantity": capacity.bottleneck_quantity,
        "marginal_profits": list(capacity.marginal_profits),
    }


class ResearchOutcomeProjector:
    """Emit research opportunities plus edge-triggered risk state changes."""

    def __init__(self, *, source: str = "research_outcome_projector") -> None:
        self.source = source
        self._blocked: dict[InstrumentId, bool] = {}

    def project(
        self,
        evaluation: ArbitrageEvaluation,
        trigger: EventEnvelope,
    ) -> tuple[EventEnvelope, ...]:
        event_time = (
            evaluation.timestamp
            if isinstance(evaluation.timestamp, datetime)
            else trigger.event_time
        )
        instrument_id = trigger.instrument_id
        quality = evaluation.quality
        blocked = quality.kill_switch or not quality.new_trades_enabled
        events: list[EventEnvelope] = []
        previous = self._blocked.get(instrument_id)
        if previous is None and blocked:
            events.append(
                self._event(
                    EventType.RISK_BLOCKED,
                    trigger,
                    event_time,
                    {
                        "kill_switch": quality.kill_switch,
                        "new_trades_enabled": quality.new_trades_enabled,
                        "quality_status": quality.status.value,
                        "reasons": list(quality.blockers),
                        "warnings": list(quality.warnings),
                        "research_only": True,
                    },
                )
            )
        elif previous is not None and previous != blocked:
            events.append(
                self._event(
                    (
                        EventType.RISK_BLOCKED
                        if blocked
                        else EventType.RISK_UNBLOCKED
                    ),
                    trigger,
                    event_time,
                    {
                        "kill_switch": quality.kill_switch,
                        "new_trades_enabled": quality.new_trades_enabled,
                        "quality_status": quality.status.value,
                        "reasons": list(quality.blockers),
                        "warnings": list(quality.warnings),
                        "research_only": True,
                    },
                )
            )
        self._blocked[instrument_id] = blocked

        directions = (
            (evaluation.creation, evaluation.creation_capacity),
            (evaluation.redemption, evaluation.redemption_capacity),
        )
        for direction, capacity in directions:
            events.append(
                self._event(
                    (
                        EventType.OPPORTUNITY_DETECTED
                        if direction.executable
                        else EventType.OPPORTUNITY_REJECTED
                    ),
                    trigger,
                    event_time,
                    {
                        "direction": direction.direction.value,
                        "cu_count": direction.cu_count,
                        "gross_profit": direction.gross_profit,
                        "estimated_costs": direction.estimated_costs,
                        "safety_buffer": direction.safety_buffer,
                        "net_profit": direction.net_profit,
                        "net_profit_bps": direction.net_profit_bps,
                        "research_executable": direction.executable,
                        "live_executable": False,
                        "rejection_reasons": list(
                            direction.rejection_reasons
                        ),
                        "capacity": _capacity_payload(capacity),
                        "internal_iopv": evaluation.internal_iopv,
                        "official_iopv": _finite_or_none(
                            evaluation.official_iopv
                        ),
                        "last_premium": _finite_or_none(
                            evaluation.last_premium
                        ),
                        "bid_premium": _finite_or_none(
                            evaluation.bid_premium
                        ),
                        "ask_premium": _finite_or_none(
                            evaluation.ask_premium
                        ),
                        "lower_bound": evaluation.lower_bound,
                        "upper_bound": evaluation.upper_bound,
                        "quality_status": quality.status.value,
                        "research_only": True,
                    },
                )
            )
        return tuple(events)

    def reset(self) -> None:
        self._blocked.clear()

    def _event(
        self,
        event_type: EventType,
        trigger: EventEnvelope,
        event_time: datetime,
        payload: dict[str, object],
    ) -> EventEnvelope:
        return EventEnvelope(
            event_type=event_type,
            event_time=event_time,
            received_time=trigger.received_time,
            source=self.source,
            instrument_id=trigger.instrument_id,
            trade_date=trigger.trade_date,
            correlation_id=trigger.correlation_id or trigger.event_id,
            causation_id=trigger.event_id,
            payload=payload,
        )


class PaperOutcomeProjector:
    """Expose paper orders, fills and primary-market emulation as events."""

    def __init__(self, *, source: str = "paper_outcome_projector") -> None:
        self.source = source

    def project(
        self,
        result: PaperEngineResult,
        trigger: EventEnvelope,
    ) -> tuple[EventEnvelope, ...]:
        cycle = result.cycle
        if cycle is None:
            return ()
        events: list[EventEnvelope] = []
        for order in cycle.orders:
            events.append(
                self._event(
                    EventType.PAPER_ORDER_CREATED,
                    trigger,
                    order.submit_time,
                    {
                        **asdict(order),
                        "live_order": False,
                        "paper_only": True,
                    },
                )
            )
        for fill in cycle.fills:
            events.append(
                self._event(
                    EventType.PAPER_ORDER_FILLED,
                    trigger,
                    fill.fill_time,
                    {
                        **asdict(fill),
                        "paper_only": True,
                    },
                )
            )

        request = result.primary_request
        if request is not None:
            request_payload = {
                **asdict(request),
                "emulated": True,
                "live_request": False,
            }
            events.append(
                self._event(
                    EventType.CREATION_REDEMPTION_REQUESTED,
                    trigger,
                    request.submit_time,
                    request_payload,
                )
            )
            if request.status == PrimaryMarketStatus.REQUEST_REJECTED:
                events.append(
                    self._event(
                        EventType.CREATION_REDEMPTION_REJECTED,
                        trigger,
                        request.confirm_time or request.submit_time,
                        request_payload,
                    )
                )
            elif request.status in {
                PrimaryMarketStatus.REQUEST_ACCEPTED,
                PrimaryMarketStatus.REQUEST_CONFIRMED,
                PrimaryMarketStatus.FINAL_CASH_SETTLED,
            }:
                events.append(
                    self._event(
                        EventType.CREATION_REDEMPTION_CONFIRMED,
                        trigger,
                        request.confirm_time or request.submit_time,
                        request_payload,
                    )
                )
            if (
                request.status == PrimaryMarketStatus.FINAL_CASH_SETTLED
                and request.final_settlement_time is not None
            ):
                events.append(
                    self._event(
                        EventType.CASH_ADJUSTMENT_SETTLED,
                        trigger,
                        request.final_settlement_time,
                        {
                            "request_id": request.request_id,
                            "cycle_id": request.cycle_id,
                            "direction": request.direction,
                            "cu_count": request.cu_count,
                            "estimated_cash_component": (
                                request.estimated_cash_component
                            ),
                            "final_cash_component": (
                                request.final_cash_component
                            ),
                            "cash_component_difference": (
                                request.final_cash_component
                                - request.estimated_cash_component
                            ),
                            "final_pnl": cycle.final_pnl,
                            "emulated": True,
                            "paper_only": True,
                        },
                    )
                )

        if cycle.rejection_reason and request is None:
            events.append(
                self._event(
                    EventType.OPPORTUNITY_REJECTED,
                    trigger,
                    cycle.fill_time or cycle.decision_time,
                    {
                        "projection_scope": "PAPER_EXECUTION",
                        "cycle_id": cycle.cycle_id,
                        "direction": cycle.direction,
                        "reason": cycle.rejection_reason,
                        "state": cycle.state.value,
                        "paper_only": True,
                    },
                )
            )
        return tuple(events)

    def _event(
        self,
        event_type: EventType,
        trigger: EventEnvelope,
        event_time: datetime,
        payload: dict[str, object],
    ) -> EventEnvelope:
        return EventEnvelope(
            event_type=event_type,
            event_time=event_time,
            received_time=trigger.received_time,
            source=self.source,
            instrument_id=trigger.instrument_id,
            trade_date=trigger.trade_date,
            correlation_id=trigger.correlation_id or trigger.event_id,
            causation_id=trigger.event_id,
            payload=payload,
        )
