"""Contracts that isolate exchange and vendor rules from research engines."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Protocol

from etf_arbitrage.data.pcf import PCFDocument
from etf_arbitrage.domain import EventEnvelope, Exchange, InstrumentId


class InstrumentCodec(Protocol):
    def decode(self, vendor_code: str) -> InstrumentId:
        """Convert a vendor symbol to an exchange-qualified ID."""

    def encode(self, instrument_id: InstrumentId) -> str:
        """Convert an internal ID to the configured vendor representation."""


class PCFParser(Protocol):
    def parse(self, path: Path | str) -> PCFDocument:
        """Parse one exchange PCF without losing raw audit metadata."""


class ExchangeCalendar(Protocol):
    def is_open(self, moment: datetime) -> bool:
        """Return whether continuous trading is open."""

    def phase(self, moment: datetime) -> str:
        """Return an exchange-specific session phase."""


class MarketDataAdapter(Protocol):
    def to_events(self, raw_message: Mapping[str, Any]) -> Iterable[EventEnvelope]:
        """Normalize one vendor message into domain events."""


class RuleBook(Protocol):
    def validate_pcf(self, pcf: PCFDocument) -> tuple[str, ...]:
        """Return explicit rule violations."""


class SettlementModel(Protocol):
    def describe(self, pcf: PCFDocument) -> Mapping[str, Any]:
        """Return account, security and cash availability constraints."""


@dataclass(frozen=True)
class ExchangeCapabilities:
    pcf_parser: bool
    instrument_codec: bool
    exchange_calendar: bool
    market_data_events: bool
    rule_book: bool
    settlement_model: bool
    production_ready: bool = False
    notes: tuple[str, ...] = ()


class ExchangeAdapter(ABC):
    exchange: Exchange
    capabilities: ExchangeCapabilities

    @property
    @abstractmethod
    def codec(self) -> InstrumentCodec:
        raise NotImplementedError

    @property
    @abstractmethod
    def pcf_parser(self) -> Optional[PCFParser]:
        raise NotImplementedError

    @property
    @abstractmethod
    def calendar(self) -> Optional[ExchangeCalendar]:
        raise NotImplementedError

    def parse_pcf(self, path: Path | str) -> PCFDocument:
        parser = self.pcf_parser
        if parser is None:
            raise NotImplementedError(
                "{} PCF parsing is not implemented".format(self.exchange.value)
            )
        return parser.parse(path)
