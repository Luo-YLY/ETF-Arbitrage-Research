"""Shanghai Stock Exchange public PCF query adapter and parser."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import gzip
import hashlib
from http.cookiejar import CookieJar
import json
from pathlib import Path
from typing import Any, Mapping, Sequence, Union
from urllib.parse import urlencode, urljoin
from urllib.request import (
    HTTPCookieProcessor,
    ProxyHandler,
    Request,
    build_opener,
)
import xml.etree.ElementTree as ET

from etf_arbitrage.domain import (
    Exchange,
    exchange_from_security_id_source,
)

from .pcf import PCFComponent, PCFDocument, PCFParseError, SubstituteFlag


SSE_PCF_QUERY_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_PCF_DETAIL_URL = (
    "https://etf.sse.com.cn/fundlist/funddetail/index.shtml?code={etf_code}"
)
SSE_PCF_HEADER_SQL_ID = "COMMON_SSE_CP_JJLB_ETFJJGK_GGSGSHQD_JBXX_C"
SSE_PCF_COMPONENT_SQL_ID = (
    "COMMON_SSE_CP_JJLB_ETFJJGK_GGSGSHQD_COMPONENT_C"
)
SSE_MANAGER_PCF_SOURCE_PAGES: Mapping[str, str] = {
    "510300": "https://www.huatai-pb.com/products/zhishu/510300/index.html#sgshqd",
    "510050": "https://www.chinaamc.com/jgb/zszq/etfssqd/?type=new&fundcode=510050",
    "510500": "https://www.nffund.com/new/personal-financing/detail.html?fundCode=510500",
    "588000": "https://www.chinaamc.com/jgb/zszq/etfssqd/?type=new&fundcode=588000",
}


class SSEPCFParser:
    """Parse official SSE public-query JSON or manager-published SSE XML."""

    parser_version = "sse-public-json-v1"

    def parse(self, path: Union[Path, str]) -> PCFDocument:
        source = Path(path)
        try:
            content = source.read_bytes()
        except OSError as exc:
            raise PCFParseError(
                "Unable to read SSE PCF: {}".format(source)
            ) from exc
        if content.lstrip().startswith(b"{"):
            try:
                payload = json.loads(content.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PCFParseError(
                    "Unable to parse SSE PCF JSON: {}".format(source)
                ) from exc
            if not isinstance(payload, Mapping):
                raise PCFParseError("SSE PCF JSON root must be an object")
            return self.parse_payload(
                payload,
                file_hash=hashlib.sha256(content).hexdigest(),
            )
        return self.parse_xml_bytes(content, source=str(source))

    def parse_payload(
        self,
        payload: Mapping[str, Any],
        *,
        file_hash: str = "",
    ) -> PCFDocument:
        header_rows = self._rows(
            payload.get("header", payload.get("basic", payload.get("result")))
        )
        component_rows = self._rows(
            payload.get("components", payload.get("component_result"))
        )
        if len(header_rows) != 1:
            raise PCFParseError(
                "SSE PCF header must contain exactly one result row"
            )
        if not component_rows:
            raise PCFParseError("SSE PCF does not contain component rows")

        header = header_rows[0]
        components = tuple(self._component(row) for row in component_rows)
        record_num = self._integer(header, "RECORD_NUM")
        if record_num != len(components):
            raise PCFParseError(
                "SSE PCF component count mismatch: RECORD_NUM={}, parsed={}".format(
                    record_num,
                    len(components),
                )
            )
        instrument_ids = [item.instrument_id for item in components]
        if len(instrument_ids) != len(set(instrument_ids)):
            raise PCFParseError(
                "SSE PCF contains duplicate exchange-qualified component securities"
            )

        etf_code = self._text(header, "TRADE_CODE")
        trading_day = self._date(header, "TRADING_DAY")
        creation_redemption = self._text(
            header,
            "CREATION_REDEMPTION",
            required=False,
        )
        versions = sorted(
            {
                str(row.get("ETF_VERSION", "")).strip()
                for row in component_rows
                if str(row.get("ETF_VERSION", "")).strip()
            }
        )
        return PCFDocument(
            version=",".join(versions) or "SSE_PUBLIC_QUERY",
            etf_code=etf_code,
            security_id_source="101",
            symbol=self._text(header, "FUND_NAME"),
            fund_management_company=self._text(header, "FUND_COMP_NAME"),
            underlying_index="",
            underlying_security_id_source="",
            creation_redemption_unit=self._integer(
                header,
                "CREATION_REDEMPTION_UNIT",
            ),
            estimate_cash_component=self._number(
                header,
                "ESTIMATED_CASH_COMPONENT",
            ),
            max_cash_ratio=self._number(header, "MAX_CASH_RATIO"),
            publish=self._yes_no(header.get("PUBLISH_IOPV")),
            creation_allowed=self._creation_allowed(creation_redemption),
            redemption_allowed=self._redemption_allowed(creation_redemption),
            record_num=record_num,
            total_record_num=record_num,
            trading_day=trading_day,
            previous_trading_day=self._date(header, "PRE_TRADING_DAY"),
            cash_component=self._number(header, "PRE_CASH_COMPONENT"),
            nav_per_creation_unit=self._number(header, "NAVPERCU"),
            nav=self._number(header, "NAV"),
            components=components,
            creation_limit=self._number(
                header,
                "CREATION_LIMIT",
                required=False,
            ),
            redemption_limit=self._number(
                header,
                "REDEMPTION_LIMIT",
                required=False,
            ),
            net_creation_limit=self._number(
                header,
                "NET_CREATION_LIMIT",
                required=False,
            ),
            net_redemption_limit=self._number(
                header,
                "NET_REDEMPTION_LIMIT",
                required=False,
            ),
            listing_exchange=Exchange.SSE.value,
            schema_version="SSE_PUBLIC_QUERY",
            parser_version=self.parser_version,
            file_hash=file_hash
            or hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest(),
            creation_redemption_mechanism=self._text(
                header,
                "CREATION_REDEMPTION_MECHANISM",
                required=False,
            )
            or "UNSPECIFIED",
            net_creation_limit_per_account=self._number(
                header,
                "NET_CREATION_LIMIT_PER_ACCT",
                required=False,
            ),
            net_redemption_limit_per_account=self._number(
                header,
                "NET_REDEMPTION_LIMIT_PER_ACCT",
                required=False,
            ),
            total_creation_limit_per_account=self._number(
                header,
                "CREATION_LIMIT_PER_ACCT",
                required=False,
            ),
            total_redemption_limit_per_account=self._number(
                header,
                "REDEMPTION_LIMIT_PER_ACCT",
                required=False,
            ),
            raw_fields={
                "source": str(payload.get("source", "SSE_PUBLIC_QUERY")),
                "fetched_at": str(payload.get("fetched_at", "")),
                "file_id": str(header.get("FILE_ID", "")),
                "etf_type": str(header.get("ETF_TYPE", "")),
                "header": dict(header),
            },
        )

    def parse_xml_bytes(
        self,
        content: bytes,
        *,
        source: str = "",
    ) -> PCFDocument:
        """Parse the official SSE XML format published by fund managers."""

        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            raise PCFParseError(
                "Unable to parse manager-published SSE PCF XML{}".format(
                    ": {}".format(source) if source else ""
                )
            ) from exc
        namespace = (
            root.tag[1:].split("}", 1)[0] if root.tag.startswith("{") else ""
        )
        local_root = root.tag.split("}")[-1]
        if local_root not in {"ETFDefinition", "SSEPortfolioCompositionFile"}:
            raise PCFParseError(
                "Unsupported SSE PCF XML root: {}".format(local_root)
            )
        q = lambda name: "{{{}}}{}".format(namespace, name) if namespace else name
        component_list = root.find(q("ComponentList"))
        if component_list is None:
            raise PCFParseError("SSE PCF XML does not contain ComponentList")
        components = tuple(
            self._xml_component(element, q)
            for element in component_list.findall(q("Component"))
        )
        record_num = self._xml_integer(root, q("RecordNumber"))
        if record_num != len(components):
            raise PCFParseError(
                "SSE PCF component count mismatch: RecordNumber={}, parsed={}".format(
                    record_num,
                    len(components),
                )
            )
        instrument_ids = [item.instrument_id for item in components]
        if len(instrument_ids) != len(set(instrument_ids)):
            raise PCFParseError(
                "SSE PCF contains duplicate exchange-qualified component securities"
            )
        creation_switch = self._xml_integer(
            root,
            q("CreationRedemptionSwitch"),
        )
        if creation_switch not in {0, 1, 2, 3}:
            raise PCFParseError(
                "Unknown SSE CreationRedemptionSwitch: {}".format(
                    creation_switch
                )
            )
        simple_fields = {
            child.tag.split("}")[-1]: (child.text or "").strip()
            for child in root
            if len(child) == 0
        }
        return PCFDocument(
            version=self._xml_optional_text(root, q("Version")) or local_root,
            etf_code=self._xml_text(root, q("FundInstrumentID")),
            security_id_source="101",
            symbol=(
                self._xml_optional_text(root, q("FundName"))
                or self._xml_optional_text(root, q("Symbol"))
                or self._xml_text(root, q("FundInstrumentID"))
            ),
            fund_management_company=(
                self._xml_optional_text(root, q("FundCompanyName"))
                or self._xml_optional_text(root, q("FundManagementCompany"))
            ),
            underlying_index=(
                self._xml_optional_text(root, q("UnderlyingIndex"))
                or self._xml_optional_text(root, q("UnderlyingSecurityID"))
            ),
            underlying_security_id_source="101",
            creation_redemption_unit=self._xml_integer(
                root,
                q("CreationRedemptionUnit"),
            ),
            estimate_cash_component=self._xml_number(
                root,
                q("EstimatedCashComponent"),
            ),
            max_cash_ratio=self._xml_number(root, q("MaxCashRatio")),
            publish=self._xml_publish_flag(
                self._xml_text(root, q("PublishIOPVFlag"))
            ),
            creation_allowed=creation_switch in {1, 2},
            redemption_allowed=creation_switch in {1, 3},
            record_num=record_num,
            total_record_num=record_num,
            trading_day=self._xml_date(root, q("TradingDay")),
            previous_trading_day=self._xml_date(root, q("PreTradingDay")),
            cash_component=self._xml_number(root, q("PreCashComponent")),
            nav_per_creation_unit=self._xml_number(root, q("NAVperCU")),
            nav=self._xml_number(root, q("NAV")),
            components=components,
            creation_limit=self._xml_optional_number(root, q("CreationLimit")),
            redemption_limit=self._xml_optional_number(
                root,
                q("RedemptionLimit"),
            ),
            net_creation_limit=self._xml_optional_number(
                root,
                q("NetCreationLimit"),
            ),
            net_redemption_limit=self._xml_optional_number(
                root,
                q("NetRedemptionLimit"),
            ),
            listing_exchange=Exchange.SSE.value,
            schema_version=local_root,
            parser_version="sse-manager-xml-v1",
            file_hash=hashlib.sha256(content).hexdigest(),
            creation_redemption_mechanism=(
                self._xml_optional_text(
                    root,
                    q("CreationRedemptionMechanism"),
                )
                or "UNSPECIFIED"
            ),
            net_creation_limit_per_account=self._xml_optional_number(
                root,
                q("NetCreationLimitPerAccount"),
            ),
            net_redemption_limit_per_account=self._xml_optional_number(
                root,
                q("NetRedemptionLimitPerAccount"),
            ),
            total_creation_limit_per_account=self._xml_optional_number(
                root,
                q("CreationLimitPerAccount"),
            ),
            total_redemption_limit_per_account=self._xml_optional_number(
                root,
                q("RedemptionLimitPerAccount"),
            ),
            raw_fields={
                "source": "SSE_MANAGER_XML",
                "source_path": source,
                "root_tag": local_root,
                "header": simple_fields,
            },
        )

    def _xml_component(self, element: ET.Element, q) -> PCFComponent:
        raw_flag = self._xml_integer(element, q("SubstitutionFlag"))
        try:
            flag = SubstituteFlag(raw_flag)
        except ValueError as exc:
            raise PCFParseError(
                "Unknown SSE SubstitutionFlag: {}".format(raw_flag)
            ) from exc
        security_id_source = self._xml_text(
            element,
            q("UnderlyingSecurityID"),
        )
        shares = self._xml_number(element, q("Quantity"))
        if shares < 0:
            raise PCFParseError("SSE PCF component quantity cannot be negative")
        substitution_amount = self._xml_optional_number(
            element,
            q("SubstitutionCashAmount"),
        )
        raw_fields = {
            child.tag.split("}")[-1]: (child.text or "").strip()
            for child in element
        }
        return PCFComponent(
            stock_code=self._xml_text(element, q("InstrumentID")),
            security_id_source=security_id_source,
            symbol=self._xml_optional_text(element, q("InstrumentName")),
            component_share=shares,
            substitute_flag=flag,
            premium_ratio=self._xml_optional_number(
                element,
                q("CreationPremiumRate"),
            ),
            creation_cash_substitute=substitution_amount,
            redemption_cash_substitute=substitution_amount,
            discount_ratio=self._xml_optional_number(
                element,
                q("RedemptionDiscountRate"),
            ),
            exchange=exchange_from_security_id_source(
                security_id_source
            ).value,
            raw_fields=raw_fields,
        )

    @staticmethod
    def _xml_optional_text(parent: ET.Element, tag: str) -> str:
        element = parent.find(tag)
        return (
            element.text.strip()
            if element is not None and element.text and element.text.strip()
            else ""
        )

    @classmethod
    def _xml_text(cls, parent: ET.Element, tag: str) -> str:
        text = cls._xml_optional_text(parent, tag)
        if not text:
            raise PCFParseError(
                "Missing required SSE PCF XML field: {}".format(
                    tag.split("}")[-1]
                )
            )
        return text

    @classmethod
    def _xml_optional_number(
        cls,
        parent: ET.Element,
        tag: str,
        default: float = 0.0,
    ) -> float:
        text = cls._xml_optional_text(parent, tag)
        if not text:
            return default
        try:
            return float(Decimal(text.replace(",", "")))
        except InvalidOperation as exc:
            raise PCFParseError(
                "Invalid numeric SSE PCF XML value for {}: {}".format(
                    tag.split("}")[-1],
                    text,
                )
            ) from exc

    @classmethod
    def _xml_number(cls, parent: ET.Element, tag: str) -> float:
        cls._xml_text(parent, tag)
        return cls._xml_optional_number(parent, tag)

    @classmethod
    def _xml_integer(cls, parent: ET.Element, tag: str) -> int:
        number = Decimal(str(cls._xml_number(parent, tag)))
        integral = number.to_integral_value()
        if number != integral:
            raise PCFParseError(
                "Expected integer SSE PCF XML value for {}: {}".format(
                    tag.split("}")[-1],
                    number,
                )
            )
        return int(integral)

    @classmethod
    def _xml_date(cls, parent: ET.Element, tag: str) -> date:
        text = cls._xml_text(parent, tag)
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except ValueError as exc:
            raise PCFParseError(
                "Invalid SSE PCF XML date for {}: {}".format(
                    tag.split("}")[-1],
                    text,
                )
            ) from exc

    @staticmethod
    def _xml_publish_flag(value: str) -> bool:
        return value.strip().upper() in {"1", "Y", "YES", "TRUE", "B"}

    def _component(self, row: Mapping[str, Any]) -> PCFComponent:
        raw_flag = self._integer(row, "SUBSTITUTION_FLAG")
        try:
            flag = SubstituteFlag(raw_flag)
        except ValueError as exc:
            raise PCFParseError(
                "Unknown SSE SUBSTITUTION_FLAG: {}".format(raw_flag)
            ) from exc
        source = self._text(row, "UNDERLYION_SECURITY_ID")
        shares = self._number(row, "QUANTITY")
        if shares < 0:
            raise PCFParseError("SSE PCF component quantity cannot be negative")
        substitution_amount = self._number(
            row,
            "SUBSTITUTION_CASH_AMOUNT",
            required=False,
        )
        return PCFComponent(
            stock_code=self._text(row, "INSTRUMENT_ID"),
            security_id_source=source,
            symbol=self._text(row, "INSTRUMENT_NAME", required=False),
            component_share=shares,
            substitute_flag=flag,
            premium_ratio=self._number(
                row,
                "CREATION_PREMIUM_RATE",
                required=False,
            ),
            creation_cash_substitute=substitution_amount,
            redemption_cash_substitute=substitution_amount,
            discount_ratio=self._number(
                row,
                "REDEMPTION_DISCOUNT_RATE",
                required=False,
            ),
            exchange=exchange_from_security_id_source(source).value,
            raw_fields=dict(row),
        )

    @staticmethod
    def _rows(value: Any) -> list[Mapping[str, Any]]:
        if isinstance(value, Mapping) and "result" in value:
            value = value["result"]
        if isinstance(value, Mapping):
            return [value]
        if isinstance(value, Sequence) and not isinstance(
            value,
            (str, bytes, bytearray),
        ):
            rows = list(value)
            if not all(isinstance(row, Mapping) for row in rows):
                raise PCFParseError("SSE PCF result rows must be objects")
            return rows
        return []

    @staticmethod
    def _text(
        value: Mapping[str, Any],
        field: str,
        *,
        required: bool = True,
    ) -> str:
        text = str(value.get(field, "")).strip()
        if text in {"", "-"}:
            if required:
                raise PCFParseError(
                    "Missing required SSE PCF field: {}".format(field)
                )
            return ""
        return text

    @classmethod
    def _number(
        cls,
        value: Mapping[str, Any],
        field: str,
        *,
        required: bool = True,
    ) -> float:
        text = cls._text(value, field, required=required)
        if not text:
            return 0.0
        normalized = (
            text.replace("￥", "")
            .replace("¥", "")
            .replace(",", "")
            .strip()
        )
        percent = normalized.endswith("%")
        if percent:
            normalized = normalized[:-1]
        try:
            number = Decimal(normalized)
        except InvalidOperation as exc:
            raise PCFParseError(
                "Invalid numeric SSE PCF value for {}: {}".format(field, text)
            ) from exc
        if percent:
            number /= Decimal("100")
        return float(number)

    @classmethod
    def _integer(cls, value: Mapping[str, Any], field: str) -> int:
        number = cls._number(value, field)
        integral = Decimal(str(number)).to_integral_value()
        if Decimal(str(number)) != integral:
            raise PCFParseError(
                "Expected integer SSE PCF value for {}: {}".format(
                    field,
                    number,
                )
            )
        return int(integral)

    @classmethod
    def _date(cls, value: Mapping[str, Any], field: str) -> date:
        text = cls._text(value, field)
        try:
            return datetime.strptime(text, "%Y%m%d").date()
        except ValueError as exc:
            raise PCFParseError(
                "Invalid SSE PCF trading date for {}: {}".format(field, text)
            ) from exc

    @staticmethod
    def _yes_no(value: Any) -> bool:
        return str(value).strip().upper() in {"Y", "YES", "TRUE", "1", "是"}

    @staticmethod
    def _creation_allowed(value: str) -> bool:
        normalized = value.replace("、", "").replace("，", "")
        return "申购" in normalized and all(
            marker not in normalized
            for marker in ("不允许申购", "暂停申购", "禁止申购")
        )

    @staticmethod
    def _redemption_allowed(value: str) -> bool:
        normalized = value.replace("、", "").replace("，", "")
        return "赎回" in normalized and all(
            marker not in normalized
            for marker in ("不允许赎回", "暂停赎回", "禁止赎回")
        )


class SSEPCFFetcher:
    """Fetch official SSE PCFs without inheriting workstation proxy settings."""

    def __init__(self, opener=None) -> None:
        self._opener = opener or build_opener(
            ProxyHandler({}),
            HTTPCookieProcessor(CookieJar()),
        )

    def fetch_payload(
        self,
        etf_code: str,
        *,
        timeout: float = 20.0,
    ) -> Mapping[str, Any]:
        code = str(etf_code).strip()
        header = self._query(SSE_PCF_HEADER_SQL_ID, code, timeout)
        components = self._query(SSE_PCF_COMPONENT_SQL_ID, code, timeout)
        return {
            "source": "SSE_PUBLIC_QUERY",
            "source_url": SSE_PCF_QUERY_URL,
            "detail_url": SSE_PCF_DETAIL_URL.format(etf_code=code),
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "header": header,
            "components": components,
        }

    def fetch_bytes(
        self,
        etf_code: str,
        *,
        trading_day: Union[date, str, None] = None,
        timeout: float = 20.0,
    ) -> bytes:
        code = str(etf_code).strip()
        if trading_day is not None and code in SSE_MANAGER_PCF_SOURCE_PAGES:
            day = self._day_text(trading_day)
            content = self._fetch_manager_xml(code, day, timeout)
            document = SSEPCFParser().parse_xml_bytes(
                content,
                source=SSE_MANAGER_PCF_SOURCE_PAGES[code],
            )
            if document.etf_code != code:
                raise PCFParseError(
                    "Official manager PCF code is {}, expected {}".format(
                        document.etf_code,
                        code,
                    )
                )
            if document.trading_day.strftime("%Y%m%d") != day:
                raise PCFParseError(
                    "Official manager PCF date is {}, expected {}".format(
                        document.trading_day.strftime("%Y%m%d"),
                        day,
                    )
                )
            return content
        payload = self.fetch_payload(code, timeout=timeout)
        if trading_day is not None:
            day = self._day_text(trading_day)
            document = SSEPCFParser().parse_payload(payload)
            actual_day = document.trading_day.strftime("%Y%m%d")
            if actual_day != day:
                raise PCFParseError(
                    "上交所公开查询当前仅返回最新清单 {}，不是所选交易日 {}；"
                    "该 ETF 尚未配置基金管理人历史 PCF 源".format(
                        actual_day,
                        day,
                    )
                )
        return json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

    def _query(
        self,
        sql_id: str,
        etf_code: str,
        timeout: float,
    ) -> Mapping[str, Any]:
        query = urlencode(
            {
                "isPagination": "false",
                "sqlId": sql_id,
                "FUNDID2": etf_code,
            }
        )
        request = Request(
            "{}?{}".format(SSE_PCF_QUERY_URL, query),
            headers={
                "User-Agent": "ETF-Arbitrage-Research/1.0",
                "Referer": SSE_PCF_DETAIL_URL.format(etf_code=etf_code),
            },
        )
        try:
            payload = json.loads(
                self._open(request, timeout).decode("utf-8-sig")
            )
        except Exception as exc:
            raise PCFParseError(
                "Unable to query official SSE PCF data for {}: {}".format(
                    etf_code,
                    exc,
                )
            ) from exc
        if not isinstance(payload, Mapping):
            raise PCFParseError("Official SSE PCF response is not an object")
        return payload

    def _fetch_manager_xml(
        self,
        etf_code: str,
        trading_day: str,
        timeout: float,
    ) -> bytes:
        try:
            if etf_code == "510300":
                return self._fetch_huatai_pcf(etf_code, trading_day, timeout)
            if etf_code in {"510050", "588000"}:
                return self._fetch_chinaamc_pcf(etf_code, trading_day, timeout)
            if etf_code == "510500":
                return self._fetch_nffund_pcf(etf_code, trading_day, timeout)
        except PCFParseError:
            raise
        except Exception as exc:
            raise PCFParseError(
                "基金管理人官方 PCF 下载失败（{}，{}）：{}".format(
                    etf_code,
                    trading_day,
                    exc,
                )
            ) from exc
        raise PCFParseError(
            "No official manager PCF source is configured for {}".format(
                etf_code
            )
        )

    def _fetch_huatai_pcf(
        self,
        etf_code: str,
        trading_day: str,
        timeout: float,
    ) -> bytes:
        page = SSE_MANAGER_PCF_SOURCE_PAGES[etf_code]
        filename = "etfd_{}_{}.xml".format(etf_code, trading_day)
        url = "https://www.huatai-pb.com/etf-web/etf/download?{}".format(
            urlencode({"filePath": filename})
        )
        return self._request_bytes(url, timeout=timeout, referer=page)

    def _fetch_chinaamc_pcf(
        self,
        etf_code: str,
        trading_day: str,
        timeout: float,
    ) -> bytes:
        page = SSE_MANAGER_PCF_SOURCE_PAGES[etf_code]
        self._request_bytes(page, timeout=timeout, referer="https://www.chinaamc.com/")
        query_url = (
            "https://www.chinaamc.com/front/front/out/etf/tradeList"
        )
        payload = self._request_json(
            query_url,
            timeout=timeout,
            referer=page,
            form={
                "fundCode": etf_code,
                "queryDate": "{}-{}-{}".format(
                    trading_day[:4],
                    trading_day[4:6],
                    trading_day[6:],
                ),
                "instType": "",
            },
        )
        if payload.get("status") != 1 or not isinstance(
            payload.get("data"),
            Mapping,
        ):
            raise PCFParseError(
                "华夏基金未返回 {} 在 {} 的官方 PCF".format(
                    etf_code,
                    trading_day,
                )
            )
        metadata = payload["data"]
        filename = str(metadata.get("fileName", "")).strip()
        expected_filename = "etfd_{}_{}.xml".format(etf_code, trading_day)
        if filename != expected_filename:
            raise PCFParseError(
                "华夏基金返回的 PCF 文件名为 {}，预期 {}".format(
                    filename or "空",
                    expected_filename,
                )
            )
        download_url = (
            "https://www.chinaamc.com/front/front/out/etf/query/etfDownload"
        )
        return self._request_bytes(
            download_url,
            timeout=timeout,
            referer=page,
            form={
                "fileName": filename,
                "year": str(metadata.get("year", trading_day[:4])),
                "fundCode": etf_code,
            },
        )

    def _fetch_nffund_pcf(
        self,
        etf_code: str,
        trading_day: str,
        timeout: float,
    ) -> bytes:
        page = SSE_MANAGER_PCF_SOURCE_PAGES[etf_code]
        self._request_bytes(page, timeout=timeout, referer="https://www.nffund.com/")
        payload = self._request_json(
            "https://www.nffund.com/nfwebApi/trade/subAndRedempList",
            timeout=timeout,
            referer=page,
            form={
                "fundCode": etf_code,
                "queryDate": "{}-{}-{}".format(
                    trading_day[:4],
                    trading_day[4:6],
                    trading_day[6:],
                ),
            },
        )
        if payload.get("code") != "ETS-5BP00000" or not isinstance(
            payload.get("data"),
            Mapping,
        ):
            raise PCFParseError(
                "南方基金未返回 {} 在 {} 的官方 PCF".format(
                    etf_code,
                    trading_day,
                )
            )
        metadata = payload["data"]
        if str(metadata.get("TradingDay", "")) != trading_day:
            raise PCFParseError(
                "南方基金返回的 PCF 日期为 {}，预期 {}".format(
                    metadata.get("TradingDay", "空"),
                    trading_day,
                )
            )
        if str(metadata.get("fundId", "")) != etf_code:
            raise PCFParseError(
                "南方基金返回的 PCF 代码为 {}，预期 {}".format(
                    metadata.get("fundId", "空"),
                    etf_code,
                )
            )
        download_path = str(metadata.get("url", "")).strip()
        if not download_path:
            raise PCFParseError("南方基金 PCF 响应缺少下载地址")
        return self._request_bytes(
            urljoin("https://www.nffund.com/", download_path),
            timeout=timeout,
            referer=page,
        )

    def _request_json(
        self,
        url: str,
        *,
        timeout: float,
        referer: str,
        form: Mapping[str, str],
    ) -> Mapping[str, Any]:
        content = self._request_bytes(
            url,
            timeout=timeout,
            referer=referer,
            form=form,
        )
        try:
            payload = json.loads(content.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PCFParseError(
                "Official manager PCF metadata response is not valid JSON"
            ) from exc
        if not isinstance(payload, Mapping):
            raise PCFParseError(
                "Official manager PCF metadata response is not an object"
            )
        return payload

    def _request_bytes(
        self,
        url: str,
        *,
        timeout: float,
        referer: str,
        form: Union[Mapping[str, str], None] = None,
    ) -> bytes:
        data = urlencode(form).encode("utf-8") if form is not None else None
        request = Request(
            url,
            data=data,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/126 Safari/537.36"
                ),
                "Referer": referer,
                "X-Requested-With": "XMLHttpRequest",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        return self._open(request, timeout)

    def _open(self, request: Request, timeout: float) -> bytes:
        last_error: Union[Exception, None] = None
        for _attempt in range(2):
            try:
                with self._opener.open(request, timeout=timeout) as response:
                    content = response.read()
                    content_encoding = str(
                        response.headers.get("Content-Encoding", "")
                    ).lower()
                if content_encoding == "gzip" or content.startswith(b"\x1f\x8b"):
                    content = gzip.decompress(content)
                if not content:
                    raise PCFParseError("Official PCF response is empty")
                return content
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    @staticmethod
    def _day_text(value: Union[date, str]) -> str:
        text = value.strftime("%Y%m%d") if isinstance(value, date) else str(value)
        try:
            datetime.strptime(text, "%Y%m%d")
        except ValueError as exc:
            raise ValueError("trading_day must use YYYYMMDD format") from exc
        return text
