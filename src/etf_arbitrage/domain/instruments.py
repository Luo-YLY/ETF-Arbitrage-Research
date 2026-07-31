"""Canonical exchange-qualified security identifiers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class Exchange(str, Enum):
    SSE = "SSE"
    SZSE = "SZSE"
    HKEX = "HKEX"
    BSE = "BSE"
    FX = "FX"
    OTHER = "OTHER"


SECURITY_ID_SOURCE_EXCHANGES = {
    "101": Exchange.SSE,
    "102": Exchange.SZSE,
    "103": Exchange.HKEX,
    "105": Exchange.FX,
    "106": Exchange.BSE,
}

VENDOR_SUFFIX_EXCHANGES = {
    ".SH": Exchange.SSE,
    ".SZ": Exchange.SZSE,
    ".HK": Exchange.HKEX,
    ".BJ": Exchange.BSE,
}

DEFAULT_VENDOR_SUFFIXES = {
    exchange: suffix for suffix, exchange in VENDOR_SUFFIX_EXCHANGES.items()
}


def exchange_from_security_id_source(value: str) -> Exchange:
    """Map exchange PCF market IDs without guessing unknown markets."""

    return SECURITY_ID_SOURCE_EXCHANGES.get(str(value).strip(), Exchange.OTHER)


def infer_etf_exchange(code: str) -> Exchange:
    """Infer the listing venue for a six-digit mainland ETF code.

    Explicit vendor suffixes always win. Bare ``5xxxxx`` ETF codes are treated
    as Shanghai listings and bare ``1xxxxx`` codes as Shenzhen listings. Other
    prefixes stay unclassified instead of being guessed.
    """

    text = str(code).strip().upper()
    for suffix, exchange in VENDOR_SUFFIX_EXCHANGES.items():
        if text.endswith(suffix):
            return exchange
    if len(text) == 6 and text.isdigit():
        if text.startswith("5"):
            return Exchange.SSE
        if text.startswith("1"):
            return Exchange.SZSE
    return Exchange.OTHER


def vendor_symbol(
    instrument_id: "InstrumentId",
    suffixes: Mapping[Exchange, str] | None = None,
) -> str:
    """Encode an exchange-qualified ID using a vendor suffix mapping."""

    mapping = dict(DEFAULT_VENDOR_SUFFIXES)
    if suffixes:
        mapping.update(
            {
                exchange
                if isinstance(exchange, Exchange)
                else Exchange(str(exchange).strip().upper()): str(suffix)
                for exchange, suffix in suffixes.items()
            }
        )
    suffix = mapping.get(instrument_id.exchange, "")
    code = instrument_id.code
    if suffix and code.upper().endswith(suffix.upper()):
        return code
    return "{}{}".format(code, suffix)


@dataclass(frozen=True, order=True)
class InstrumentId:
    """A security code qualified by its trading venue."""

    exchange: Exchange
    code: str

    def __post_init__(self) -> None:
        exchange = (
            self.exchange
            if isinstance(self.exchange, Exchange)
            else Exchange(str(self.exchange).strip().upper())
        )
        code = str(self.code).strip().upper()
        if not code:
            raise ValueError("Instrument code cannot be empty")
        if ":" in code:
            raise ValueError("Instrument code cannot contain ':'")
        object.__setattr__(self, "exchange", exchange)
        object.__setattr__(self, "code", code)

    @property
    def key(self) -> str:
        return "{}:{}".format(self.exchange.value, self.code)

    @property
    def legacy_code(self) -> str:
        """Compatibility key for the current single-market pricing engines."""

        return self.code

    def to_dict(self) -> dict[str, str]:
        return {"exchange": self.exchange.value, "code": self.code}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "InstrumentId":
        return cls(exchange=Exchange(str(value["exchange"]).upper()), code=str(value["code"]))

    @classmethod
    def parse(
        cls,
        value: str,
        default_exchange: Exchange | str | None = None,
    ) -> "InstrumentId":
        text = str(value).strip().upper()
        if ":" in text:
            exchange, code = text.split(":", 1)
            return cls(Exchange(exchange.strip().upper()), code)
        for suffix, exchange in VENDOR_SUFFIX_EXCHANGES.items():
            if text.endswith(suffix):
                return cls(exchange, text[: -len(suffix)])
        if default_exchange is None:
            raise ValueError("Unqualified instrument requires default_exchange")
        exchange = (
            default_exchange
            if isinstance(default_exchange, Exchange)
            else Exchange(str(default_exchange).upper())
        )
        return cls(exchange, text)

    def __str__(self) -> str:
        return self.key
