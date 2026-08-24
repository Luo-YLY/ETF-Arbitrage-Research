"""Shenzhen Stock Exchange PCF XML models and parser."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import IntEnum
import hashlib
from pathlib import Path
from typing import Any, List, Mapping, Union
import xml.etree.ElementTree as ET

from etf_arbitrage.domain import (
    Exchange,
    InstrumentId,
    exchange_from_security_id_source,
)

from .models import ComponentWeight, ETFInfo


SZSE_PCF_NAMESPACE = "http://ts.szse.cn/Fund"


class PCFParseError(ValueError):
    """Raised when a PCF file is malformed or internally inconsistent."""


class SubstituteFlag(IntEnum):
    PROHIBITED = 0
    ALLOWED = 1
    MANDATORY = 2
    CROSS_MARKET_REFUND = 3
    CROSS_MARKET_MANDATORY = 4
    REFUND = 5
    CASH_MANDATORY = 6
    HK_REFUND = 7
    HK_MANDATORY = 8

    @property
    def requires_cash_substitution(self) -> bool:
        """Whether the component is settled through cash rather than delivery."""

        return self.value >= self.MANDATORY.value


@dataclass(frozen=True)
class PCFComponent:
    stock_code: str
    security_id_source: str
    symbol: str
    component_share: float
    substitute_flag: SubstituteFlag
    premium_ratio: float
    creation_cash_substitute: float
    redemption_cash_substitute: float
    discount_ratio: float = 0.0
    exchange: str = ""
    raw_fields: Mapping[str, Any] = field(
        default_factory=dict, compare=False, repr=False
    )

    @property
    def instrument_id(self) -> InstrumentId:
        exchange = (
            self.exchange
            if isinstance(self.exchange, Exchange)
            else Exchange(str(self.exchange).upper())
            if self.exchange
            else exchange_from_security_id_source(self.security_id_source)
        )
        return InstrumentId(exchange, self.stock_code)

    @property
    def is_virtual_subscription_cash(self) -> bool:
        """Whether this is the zero-share ``159900 申赎现金`` helper row.

        Some PCFs publish the settlement helper as a mandatory cash component.
        Its amount is already represented by the PCF cash component and must not
        be priced as an additional constituent.
        """

        normalized_symbol = "".join(str(self.symbol).split())
        return self.component_share == 0 and (
            self.stock_code == "159900" or normalized_symbol == "申赎现金"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "stock_code": self.stock_code,
            "security_id_source": self.security_id_source,
            "symbol": self.symbol,
            "component_share": self.component_share,
            "substitute_flag": int(self.substitute_flag),
            "premium_ratio": self.premium_ratio,
            "creation_cash_substitute": self.creation_cash_substitute,
            "redemption_cash_substitute": self.redemption_cash_substitute,
            "discount_ratio": self.discount_ratio,
            "exchange": self.instrument_id.exchange.value,
            "raw_fields": dict(self.raw_fields),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PCFComponent":
        return cls(
            stock_code=str(value["stock_code"]),
            security_id_source=str(value["security_id_source"]),
            symbol=str(value["symbol"]),
            component_share=float(value["component_share"]),
            substitute_flag=SubstituteFlag(int(value["substitute_flag"])),
            premium_ratio=float(value.get("premium_ratio", 0.0)),
            creation_cash_substitute=float(
                value.get("creation_cash_substitute", 0.0)
            ),
            redemption_cash_substitute=float(
                value.get("redemption_cash_substitute", 0.0)
            ),
            discount_ratio=float(value.get("discount_ratio", 0.0)),
            exchange=str(value.get("exchange", "")),
            raw_fields=dict(value.get("raw_fields", {})),
        )


@dataclass(frozen=True)
class PCFDocument:
    version: str
    etf_code: str
    security_id_source: str
    symbol: str
    fund_management_company: str
    underlying_index: str
    underlying_security_id_source: str
    creation_redemption_unit: int
    estimate_cash_component: float
    max_cash_ratio: float
    publish: bool
    creation_allowed: bool
    redemption_allowed: bool
    record_num: int
    total_record_num: int
    trading_day: date
    previous_trading_day: date
    cash_component: float
    nav_per_creation_unit: float
    nav: float
    components: tuple[PCFComponent, ...]
    creation_limit: float = 0.0
    redemption_limit: float = 0.0
    net_creation_limit: float = 0.0
    net_redemption_limit: float = 0.0
    listing_exchange: str = "SZSE"
    schema_version: str = ""
    parser_version: str = "szse-xml-v1"
    file_hash: str = ""
    creation_redemption_mechanism: str = "UNSPECIFIED"
    net_creation_limit_per_account: float = 0.0
    net_redemption_limit_per_account: float = 0.0
    total_creation_limit_per_account: float = 0.0
    total_redemption_limit_per_account: float = 0.0
    account_requirements: tuple[str, ...] = ()
    settlement_rules: tuple[str, ...] = ()
    raw_fields: Mapping[str, Any] = field(
        default_factory=dict, compare=False, repr=False
    )

    @property
    def etf_id(self) -> InstrumentId:
        exchange = (
            self.listing_exchange
            if isinstance(self.listing_exchange, Exchange)
            else Exchange(str(self.listing_exchange).upper())
        )
        return InstrumentId(exchange, self.etf_code)

    @property
    def event_version(self) -> str:
        return self.file_hash or "{}:{}:{}".format(
            self.version,
            self.trading_day.isoformat(),
            self.etf_id.key,
        )

    def component_weights(self) -> List[ComponentWeight]:
        """Return quantity weights used to request all PCF component quotes."""
        return [
            ComponentWeight(
                self.etf_code,
                component.stock_code,
                component.component_share,
                component.instrument_id.exchange.value,
            )
            for component in self.components
        ]

    def to_etf_info(self) -> ETFInfo:
        return ETFInfo(
            etf_code=self.etf_code,
            name=self.symbol,
            exchange=self.listing_exchange,
            tracking_index=self.underlying_index,
            shares=float(self.creation_redemption_unit),
            creation_unit=self.creation_redemption_unit,
            cash_component=self.estimate_cash_component,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "etf_code": self.etf_code,
            "security_id_source": self.security_id_source,
            "symbol": self.symbol,
            "fund_management_company": self.fund_management_company,
            "underlying_index": self.underlying_index,
            "underlying_security_id_source": self.underlying_security_id_source,
            "creation_redemption_unit": self.creation_redemption_unit,
            "estimate_cash_component": self.estimate_cash_component,
            "max_cash_ratio": self.max_cash_ratio,
            "publish": self.publish,
            "creation_allowed": self.creation_allowed,
            "redemption_allowed": self.redemption_allowed,
            "record_num": self.record_num,
            "total_record_num": self.total_record_num,
            "trading_day": self.trading_day.isoformat(),
            "previous_trading_day": self.previous_trading_day.isoformat(),
            "cash_component": self.cash_component,
            "nav_per_creation_unit": self.nav_per_creation_unit,
            "nav": self.nav,
            "components": [component.to_dict() for component in self.components],
            "creation_limit": self.creation_limit,
            "redemption_limit": self.redemption_limit,
            "net_creation_limit": self.net_creation_limit,
            "net_redemption_limit": self.net_redemption_limit,
            "listing_exchange": self.listing_exchange,
            "schema_version": self.schema_version,
            "parser_version": self.parser_version,
            "file_hash": self.file_hash,
            "creation_redemption_mechanism": self.creation_redemption_mechanism,
            "net_creation_limit_per_account": self.net_creation_limit_per_account,
            "net_redemption_limit_per_account": self.net_redemption_limit_per_account,
            "total_creation_limit_per_account": self.total_creation_limit_per_account,
            "total_redemption_limit_per_account": self.total_redemption_limit_per_account,
            "account_requirements": list(self.account_requirements),
            "settlement_rules": list(self.settlement_rules),
            "raw_fields": dict(self.raw_fields),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PCFDocument":
        return cls(
            version=str(value["version"]),
            etf_code=str(value["etf_code"]),
            security_id_source=str(value["security_id_source"]),
            symbol=str(value["symbol"]),
            fund_management_company=str(value["fund_management_company"]),
            underlying_index=str(value["underlying_index"]),
            underlying_security_id_source=str(
                value["underlying_security_id_source"]
            ),
            creation_redemption_unit=int(value["creation_redemption_unit"]),
            estimate_cash_component=float(value["estimate_cash_component"]),
            max_cash_ratio=float(value["max_cash_ratio"]),
            publish=bool(value["publish"]),
            creation_allowed=bool(value["creation_allowed"]),
            redemption_allowed=bool(value["redemption_allowed"]),
            record_num=int(value["record_num"]),
            total_record_num=int(value["total_record_num"]),
            trading_day=date.fromisoformat(str(value["trading_day"])),
            previous_trading_day=date.fromisoformat(
                str(value["previous_trading_day"])
            ),
            cash_component=float(value["cash_component"]),
            nav_per_creation_unit=float(value["nav_per_creation_unit"]),
            nav=float(value["nav"]),
            components=tuple(
                PCFComponent.from_dict(component)
                for component in value.get("components", ())
            ),
            creation_limit=float(value.get("creation_limit", 0.0)),
            redemption_limit=float(value.get("redemption_limit", 0.0)),
            net_creation_limit=float(value.get("net_creation_limit", 0.0)),
            net_redemption_limit=float(value.get("net_redemption_limit", 0.0)),
            listing_exchange=str(value.get("listing_exchange", "SZSE")),
            schema_version=str(value.get("schema_version", "")),
            parser_version=str(value.get("parser_version", "unknown")),
            file_hash=str(value.get("file_hash", "")),
            creation_redemption_mechanism=str(
                value.get("creation_redemption_mechanism", "UNSPECIFIED")
            ),
            net_creation_limit_per_account=float(
                value.get("net_creation_limit_per_account", 0.0)
            ),
            net_redemption_limit_per_account=float(
                value.get("net_redemption_limit_per_account", 0.0)
            ),
            total_creation_limit_per_account=float(
                value.get("total_creation_limit_per_account", 0.0)
            ),
            total_redemption_limit_per_account=float(
                value.get("total_redemption_limit_per_account", 0.0)
            ),
            account_requirements=tuple(value.get("account_requirements", ())),
            settlement_rules=tuple(value.get("settlement_rules", ())),
            raw_fields=dict(value.get("raw_fields", {})),
        )


class SZSEPCFParser:
    """Parse the public SZSE PCF XML format without vendor dependencies."""

    def parse(self, path: Union[Path, str]) -> PCFDocument:
        source = Path(path)
        try:
            content = source.read_bytes()
            root = ET.fromstring(content)
        except (ET.ParseError, OSError) as exc:
            raise PCFParseError("Unable to parse PCF XML: {}".format(source)) from exc

        namespace = self._namespace(root.tag)
        if namespace and namespace != SZSE_PCF_NAMESPACE:
            raise PCFParseError("Unsupported PCF namespace: {}".format(namespace))
        q = lambda name: "{{{}}}{}".format(namespace, name) if namespace else name

        components_parent = root.find(q("Components"))
        if components_parent is None:
            raise PCFParseError("PCF does not contain Components")
        components = tuple(
            self._parse_component(element, q)
            for element in components_parent.findall(q("Component"))
        )
        record_num = self._integer(root, q("RecordNum"))
        total_record_num = self._integer(root, q("TotalRecordNum"))
        local_component_count = sum(
            component.security_id_source
            == self._text(root, q("SecurityIDSource"))
            for component in components
        )
        if (
            total_record_num != len(components)
            or record_num not in {len(components), local_component_count}
        ):
            raise PCFParseError(
                "PCF component count mismatch: RecordNum={}, local={}, "
                "TotalRecordNum={}, parsed={}".format(
                    record_num,
                    local_component_count,
                    total_record_num,
                    len(components),
                )
            )
        instrument_ids = [component.instrument_id for component in components]
        if len(instrument_ids) != len(set(instrument_ids)):
            raise PCFParseError(
                "PCF contains duplicate exchange-qualified component securities"
            )

        creation_unit = self._integer(root, q("CreationRedemptionUnit"))
        if creation_unit <= 0:
            raise PCFParseError("CreationRedemptionUnit must be positive")
        return PCFDocument(
            version=self._text(root, q("Version")),
            etf_code=self._text(root, q("SecurityID")),
            security_id_source=self._text(root, q("SecurityIDSource")),
            symbol=self._text(root, q("Symbol")),
            fund_management_company=self._text(root, q("FundManagementCompany")),
            underlying_index=self._text(root, q("UnderlyingSecurityID")),
            underlying_security_id_source=self._text(
                root, q("UnderlyingSecurityIDSource")
            ),
            creation_redemption_unit=creation_unit,
            estimate_cash_component=self._number(root, q("EstimateCashComponent")),
            max_cash_ratio=self._number(root, q("MaxCashRatio")),
            publish=self._yes_no(root, q("Publish")),
            creation_allowed=self._yes_no(root, q("Creation")),
            redemption_allowed=self._yes_no(root, q("Redemption")),
            record_num=record_num,
            total_record_num=total_record_num,
            trading_day=self._date(root, q("TradingDay")),
            previous_trading_day=self._date(root, q("PreTradingDay")),
            cash_component=self._number(root, q("CashComponent")),
            nav_per_creation_unit=self._number(root, q("NAVperCU")),
            nav=self._number(root, q("NAV")),
            components=components,
            creation_limit=self._optional_number(root, q("CreationLimit")),
            redemption_limit=self._optional_number(root, q("RedemptionLimit")),
            net_creation_limit=self._optional_number(root, q("NetCreationLimit")),
            net_redemption_limit=self._optional_number(root, q("NetRedemptionLimit")),
            listing_exchange=Exchange.SZSE.value,
            schema_version=self._text(root, q("Version")),
            parser_version="szse-xml-v1",
            file_hash=hashlib.sha256(content).hexdigest(),
        )

    def _parse_component(self, element: ET.Element, q) -> PCFComponent:
        # SZSE compact PCFs omit fields that are not applicable to a substitute flag.
        shares = self._optional_number(element, q("ComponentShare"))
        if shares < 0:
            raise PCFParseError("ComponentShare cannot be negative")
        raw_flag = self._integer(element, q("SubstituteFlag"))
        try:
            flag = SubstituteFlag(raw_flag)
        except ValueError as exc:
            raise PCFParseError("Unknown SubstituteFlag: {}".format(raw_flag)) from exc
        security_id_source = self._text(
            element, q("UnderlyingSecurityIDSource")
        )
        return PCFComponent(
            stock_code=self._text(element, q("UnderlyingSecurityID")),
            security_id_source=security_id_source,
            symbol=self._text(element, q("UnderlyingSymbol")),
            component_share=shares,
            substitute_flag=flag,
            premium_ratio=self._optional_number(element, q("PremiumRatio")),
            creation_cash_substitute=self._optional_number(
                element, q("CreationCashSubstitute")
            ),
            redemption_cash_substitute=self._optional_number(
                element, q("RedemptionCashSubstitute")
            ),
            discount_ratio=self._optional_number(element, q("DiscountRatio")),
            exchange=exchange_from_security_id_source(
                security_id_source
            ).value,
        )

    @staticmethod
    def _namespace(tag: str) -> str:
        return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""

    @staticmethod
    def _text(parent: ET.Element, tag: str) -> str:
        element = parent.find(tag)
        if element is None or element.text is None or not element.text.strip():
            raise PCFParseError("Missing required PCF field: {}".format(tag.split("}")[-1]))
        return element.text.strip()

    @classmethod
    def _decimal(cls, parent: ET.Element, tag: str) -> Decimal:
        text = cls._text(parent, tag)
        try:
            return Decimal(text)
        except InvalidOperation as exc:
            raise PCFParseError("Invalid numeric PCF value: {}".format(text)) from exc

    @classmethod
    def _number(cls, parent: ET.Element, tag: str) -> float:
        return float(cls._decimal(parent, tag))

    @staticmethod
    def _optional_number(parent: ET.Element, tag: str, default: float = 0.0) -> float:
        element = parent.find(tag)
        if element is None or element.text is None or not element.text.strip():
            return default
        text = element.text.strip()
        try:
            return float(Decimal(text))
        except InvalidOperation as exc:
            raise PCFParseError("Invalid numeric PCF value: {}".format(text)) from exc

    @classmethod
    def _integer(cls, parent: ET.Element, tag: str) -> int:
        value = cls._decimal(parent, tag)
        integral = value.to_integral_value()
        if value != integral:
            raise PCFParseError("Expected integer PCF value: {}".format(value))
        return int(integral)

    @classmethod
    def _date(cls, parent: ET.Element, tag: str) -> date:
        text = cls._text(parent, tag)
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except ValueError as exc:
            raise PCFParseError("Invalid PCF trading date: {}".format(text)) from exc

    @classmethod
    def _yes_no(cls, parent: ET.Element, tag: str) -> bool:
        text = cls._text(parent, tag).upper()
        if text not in {"Y", "N"}:
            raise PCFParseError("Expected Y/N PCF value: {}".format(text))
        return text == "Y"
