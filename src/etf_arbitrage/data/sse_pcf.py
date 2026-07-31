"""Shanghai Stock Exchange public PCF query adapter and parser."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence, Union
from urllib.parse import urlencode
from urllib.request import Request, urlopen

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


class SSEPCFParser:
    """Parse the JSON returned by the SSE public ETF PCF query endpoints."""

    parser_version = "sse-public-json-v1"

    def parse(self, path: Union[Path, str]) -> PCFDocument:
        source = Path(path)
        try:
            content = source.read_bytes()
            payload = json.loads(content.decode("utf-8-sig"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PCFParseError(
                "Unable to parse SSE PCF JSON: {}".format(source)
            ) from exc
        if not isinstance(payload, Mapping):
            raise PCFParseError("SSE PCF JSON root must be an object")
        return self.parse_payload(
            payload,
            file_hash=hashlib.sha256(content).hexdigest(),
        )

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
    """Fetch the latest SSE PCF header and component payloads."""

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
        timeout: float = 20.0,
    ) -> bytes:
        return json.dumps(
            self.fetch_payload(etf_code, timeout=timeout),
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

    @staticmethod
    def _query(sql_id: str, etf_code: str, timeout: float) -> Mapping[str, Any]:
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
            with urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8-sig"))
        except Exception as exc:
            raise PCFParseError(
                "Unable to query official SSE PCF data for {}".format(etf_code)
            ) from exc
        if not isinstance(payload, Mapping):
            raise PCFParseError("Official SSE PCF response is not an object")
        return payload

