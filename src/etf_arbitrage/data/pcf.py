"""Shenzhen Stock Exchange PCF XML models and parser."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import IntEnum
from pathlib import Path
from typing import List, Union
import xml.etree.ElementTree as ET

from .models import ComponentWeight, ETFInfo


SZSE_PCF_NAMESPACE = "http://ts.szse.cn/Fund"


class PCFParseError(ValueError):
    """Raised when a PCF file is malformed or internally inconsistent."""


class SubstituteFlag(IntEnum):
    PROHIBITED = 0
    ALLOWED = 1
    MANDATORY = 2


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

    def component_weights(self) -> List[ComponentWeight]:
        """Return quantity weights used to request all PCF component quotes."""
        return [
            ComponentWeight(self.etf_code, component.stock_code, component.component_share)
            for component in self.components
        ]

    def to_etf_info(self) -> ETFInfo:
        return ETFInfo(
            etf_code=self.etf_code,
            name=self.symbol,
            exchange="SZSE",
            tracking_index=self.underlying_index,
            shares=float(self.creation_redemption_unit),
            creation_unit=self.creation_redemption_unit,
            cash_component=self.estimate_cash_component,
        )


class SZSEPCFParser:
    """Parse the public SZSE PCF XML format without vendor dependencies."""

    def parse(self, path: Union[Path, str]) -> PCFDocument:
        source = Path(path)
        try:
            root = ET.parse(source).getroot()
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
        if record_num != len(components) or total_record_num != len(components):
            raise PCFParseError(
                "PCF component count mismatch: RecordNum={}, TotalRecordNum={}, parsed={}".format(
                    record_num, total_record_num, len(components)
                )
            )
        codes = [component.stock_code for component in components]
        if len(codes) != len(set(codes)):
            raise PCFParseError("PCF contains duplicate component security codes")

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
        return PCFComponent(
            stock_code=self._text(element, q("UnderlyingSecurityID")),
            security_id_source=self._text(element, q("UnderlyingSecurityIDSource")),
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
