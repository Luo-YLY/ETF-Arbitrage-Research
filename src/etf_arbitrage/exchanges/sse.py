"""Shanghai exchange adapter backed by the public SSE PCF query format."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from etf_arbitrage.data import SSEPCFParser
from etf_arbitrage.domain import Exchange, InstrumentId
from etf_arbitrage.operations import SZSEMarketSchedule

from .base import ExchangeAdapter, ExchangeCalendar, ExchangeCapabilities, PCFParser


@dataclass(frozen=True)
class SSEInstrumentCodec:
    suffix: str = ".SH"

    def decode(self, vendor_code: str) -> InstrumentId:
        value = str(vendor_code).strip().upper()
        suffix = self.suffix.upper()
        if suffix and value.endswith(suffix):
            value = value[: -len(suffix)]
        return InstrumentId(Exchange.SSE, value)

    def encode(self, instrument_id: InstrumentId) -> str:
        if instrument_id.exchange != Exchange.SSE:
            raise ValueError("SSE codec cannot encode {}".format(instrument_id))
        return "{}{}".format(instrument_id.code, self.suffix)


class SSEAdapter(ExchangeAdapter):
    exchange = Exchange.SSE
    capabilities = ExchangeCapabilities(
        pcf_parser=True,
        instrument_codec=True,
        exchange_calendar=True,
        market_data_events=False,
        rule_book=False,
        settlement_model=False,
        production_ready=False,
        notes=(
            "SSE public JSON and fund-manager historical XML PCF parsing is implemented.",
            "Exchange-qualified symbol encoding supports the .SH suffix.",
            "Mainland session clock is enabled for fail-closed phase gating.",
            "Account, settlement and production trading rules are not implemented.",
        ),
    )

    def __init__(self, suffix: str = ".SH") -> None:
        self._codec = SSEInstrumentCodec(suffix=suffix)
        self._pcf_parser = SSEPCFParser()
        self._calendar = SZSEMarketSchedule()

    @property
    def codec(self) -> SSEInstrumentCodec:
        return self._codec

    @property
    def pcf_parser(self) -> PCFParser:
        return self._pcf_parser

    @property
    def calendar(self) -> Optional[ExchangeCalendar]:
        return self._calendar
