"""Runtime registry for exchange-specific behavior and capability inspection."""

from __future__ import annotations

from typing import Iterable

from etf_arbitrage.domain import Exchange

from .base import ExchangeAdapter


class ExchangeAdapterRegistry:
    """Keep venue behavior behind an exchange-qualified lookup boundary."""

    def __init__(self, adapters: Iterable[ExchangeAdapter] = ()) -> None:
        self._adapters: dict[Exchange, ExchangeAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(
        self,
        adapter: ExchangeAdapter,
        *,
        replace: bool = False,
    ) -> None:
        exchange = adapter.exchange
        if exchange in self._adapters and not replace:
            raise ValueError(
                "{} adapter is already registered".format(exchange.value)
            )
        self._adapters[exchange] = adapter

    def get(self, exchange: Exchange | str) -> ExchangeAdapter:
        normalized = (
            exchange
            if isinstance(exchange, Exchange)
            else Exchange(str(exchange).upper())
        )
        try:
            return self._adapters[normalized]
        except KeyError as exc:
            raise KeyError(
                "No adapter registered for {}".format(normalized.value)
            ) from exc

    def supports(self, exchange: Exchange | str) -> bool:
        normalized = (
            exchange
            if isinstance(exchange, Exchange)
            else Exchange(str(exchange).upper())
        )
        return normalized in self._adapters

    def capability_matrix(self) -> dict[str, dict[str, object]]:
        """Return a serializable, explicit implementation-gap matrix."""

        result: dict[str, dict[str, object]] = {}
        for exchange, adapter in sorted(
            self._adapters.items(), key=lambda item: item[0].value
        ):
            capabilities = adapter.capabilities
            result[exchange.value] = {
                "pcf_parser": capabilities.pcf_parser,
                "instrument_codec": capabilities.instrument_codec,
                "exchange_calendar": capabilities.exchange_calendar,
                "market_data_events": capabilities.market_data_events,
                "rule_book": capabilities.rule_book,
                "settlement_model": capabilities.settlement_model,
                "production_ready": capabilities.production_ready,
                "notes": list(capabilities.notes),
            }
        return result

    @property
    def exchanges(self) -> tuple[Exchange, ...]:
        return tuple(sorted(self._adapters, key=lambda item: item.value))


def default_exchange_registry() -> ExchangeAdapterRegistry:
    """Build the current registry without implying production readiness."""

    from .sse import SSEAdapter
    from .szse import SZSEAdapter

    return ExchangeAdapterRegistry((SZSEAdapter(), SSEAdapter()))
