"""Exchange-calendar observations projected into auditable session events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from etf_arbitrage.domain import EventEnvelope, EventType, Exchange, InstrumentId
from etf_arbitrage.exchanges import ExchangeAdapterRegistry


@dataclass
class _SessionState:
    trade_date: date
    phase: Optional[str] = None
    started: bool = False
    ended: bool = False


class MarketSessionEventGenerator:
    """Emit transitions only; polling the same phase is idempotent."""

    def __init__(
        self,
        registry: ExchangeAdapterRegistry,
        *,
        source: str = "market_session_clock",
    ) -> None:
        self.registry = registry
        self.source = source
        self._states: dict[Exchange, _SessionState] = {}

    def observe(
        self,
        exchange: Exchange | str,
        moment: datetime,
        *,
        received_time: Optional[datetime] = None,
    ) -> tuple[EventEnvelope, ...]:
        normalized = (
            exchange
            if isinstance(exchange, Exchange)
            else Exchange(str(exchange).upper())
        )
        adapter = self.registry.get(normalized)
        calendar = adapter.calendar
        if calendar is None:
            raise NotImplementedError(
                "{} exchange calendar is not implemented".format(
                    normalized.value
                )
            )

        trade_date = moment.date()
        phase = str(calendar.phase(moment))
        state = self._states.get(normalized)
        if state is None or state.trade_date != trade_date:
            state = _SessionState(trade_date=trade_date)
            self._states[normalized] = state

        market_id = InstrumentId(normalized, "__MARKET__")
        received = received_time or moment
        events: list[EventEnvelope] = []
        if not state.started and phase not in {"closed", "after_close"}:
            events.append(
                self._event(
                    EventType.TRADING_DAY_STARTED,
                    market_id,
                    moment,
                    received,
                    trade_date,
                    {
                        "phase": phase,
                        "calendar_observation": True,
                    },
                )
            )
            state.started = True

        if phase != state.phase:
            events.append(
                self._event(
                    EventType.SESSION_PHASE_CHANGED,
                    market_id,
                    moment,
                    received,
                    trade_date,
                    {
                        "previous_phase": state.phase,
                        "phase": phase,
                        "market_open": bool(calendar.is_open(moment)),
                        "calendar_observation": True,
                    },
                )
            )
            state.phase = phase

        if phase == "after_close" and not state.ended:
            events.append(
                self._event(
                    EventType.TRADING_DAY_ENDED,
                    market_id,
                    moment,
                    received,
                    trade_date,
                    {
                        "phase": phase,
                        "calendar_observation": True,
                    },
                )
            )
            state.ended = True
        return tuple(events)

    def reset(self) -> None:
        self._states.clear()

    def _event(
        self,
        event_type: EventType,
        instrument_id: InstrumentId,
        event_time: datetime,
        received_time: datetime,
        trade_date: date,
        payload: dict[str, object],
    ) -> EventEnvelope:
        return EventEnvelope(
            event_type=event_type,
            event_time=event_time,
            received_time=received_time,
            source=self.source,
            instrument_id=instrument_id,
            trade_date=trade_date,
            payload=payload,
        )
