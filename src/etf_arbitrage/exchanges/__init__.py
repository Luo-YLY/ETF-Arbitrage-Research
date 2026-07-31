"""Exchange-specific adapters behind stable cross-market contracts."""

from .base import (
    ExchangeAdapter,
    ExchangeCalendar,
    ExchangeCapabilities,
    InstrumentCodec,
    MarketDataAdapter,
    PCFParser,
    RuleBook,
    SettlementModel,
)
from .sse import SSEAdapter
from .szse import SZSEAdapter
from .registry import ExchangeAdapterRegistry, default_exchange_registry

__all__ = [
    "ExchangeAdapter",
    "ExchangeAdapterRegistry",
    "ExchangeCalendar",
    "ExchangeCapabilities",
    "InstrumentCodec",
    "MarketDataAdapter",
    "PCFParser",
    "RuleBook",
    "SSEAdapter",
    "SZSEAdapter",
    "SettlementModel",
    "default_exchange_registry",
]
