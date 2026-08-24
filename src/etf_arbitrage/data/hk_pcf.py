"""Official Tracker Fund of Hong Kong daily PCF download and parsing.

The public TraHK website exposes the daily Portfolio Composition File as an
XLSX workbook.  This module parses the workbook with the Python standard
library so the research environment does not need an Excel dependency.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
import posixpath
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Union
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from zipfile import BadZipFile, ZipFile

from etf_arbitrage.domain import Exchange

from .pcf import PCFComponent, PCFDocument, PCFParseError, SubstituteFlag


TRAHK_PCF_SOURCE_PAGE = (
    "https://www.trahk.com.hk/en-hk/trahk-fund/fund-information/"
)
TRAHK_PCF_DOWNLOAD_URL = (
    "https://rbwm-api.hsbc.com.hk/"
    "pws-hk-hase-hsvm2-papi-prod-proxy/v1/hsvm/csv/trahkfund/holdings"
)
TRAHK_HKD_COUNTER = "2800"
TRAHK_RMB_COUNTER = "82800"
TRAHK_COUNTERS = (TRAHK_HKD_COUNTER, TRAHK_RMB_COUNTER)

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CELL_REFERENCE = re.compile(r"^([A-Z]+)(\d+)$")
_EXCEL_EPOCH = datetime(1899, 12, 30)
_MAX_WORKBOOK_BYTES = 10 * 1024 * 1024
_MAX_UNCOMPRESSED_BYTES = 30 * 1024 * 1024


class HKPCFValidationError(ValueError):
    """Raised when an official Hong Kong PCF does not match the request."""


def normalize_trahk_counter(value: str) -> str:
    """Normalize vendor-style TraHK counter identifiers."""

    text = str(value).strip().upper()
    if ":" in text:
        exchange, text = text.split(":", 1)
        if exchange not in {"HKEX", "HK"}:
            raise HKPCFValidationError("TraHK counter must be listed on HKEX")
    if text.endswith(".HK"):
        text = text[:-3]
    text = text.lstrip("0") or "0"
    if text not in TRAHK_COUNTERS:
        raise HKPCFValidationError(
            "Only TraHK counters 2800 and 82800 are supported by this PCF source"
        )
    return text


class HKPCFParser:
    """Parse the official TraHK daily PCF Detail XLSX workbook."""

    parser_version = "trahk-xlsx-v1"

    def parse(self, path: Union[Path, str]) -> PCFDocument:
        source = Path(path)
        try:
            content = source.read_bytes()
        except OSError as exc:
            raise PCFParseError("Unable to read TraHK PCF: {}".format(source)) from exc
        return self.parse_bytes(content)

    def parse_bytes(self, content: bytes) -> PCFDocument:
        if not content or len(content) > _MAX_WORKBOOK_BYTES:
            raise PCFParseError("TraHK PCF workbook is empty or unexpectedly large")
        rows = _read_first_worksheet(content)
        file_hash = sha256(content).hexdigest()

        manager = _next_value(_required_row(rows, "Name of ETF Manager"), 0)
        fund_name = _next_value(_required_row(rows, "Name of ETF"), 0)
        counters = _counter_codes(_required_row(rows, "Stock Code"))
        if not set(TRAHK_COUNTERS).issubset(counters):
            raise PCFParseError(
                "TraHK PCF does not contain both 2800 and 82800 counters"
            )

        valuation_date = _first_date(
            _required_row(rows, "Date (ddmmmyyyy)"), start=1
        )
        basket_date = _first_date(
            _required_row(rows, "Index Basket Shares per Creation Unit as of"),
            start=1,
        )

        nav_per_unit = _currency_values(
            _required_row(rows, "N.A.V. per Unit in Trading Currency (Note 1)")
        )
        nav_per_creation = _currency_values(
            _required_row(rows, "N.A.V. per Creation Unit (Note 2)")
        )
        actual_cash = _currency_values(
            _required_row(rows, "Actual Cash per Creation Unit (Note 3)")
        )
        dividend_per_unit = _currency_values(
            _required_row(rows, "Dividend Equivalent Amount per Unit (Note 6)")
        )
        dividend_per_creation = _currency_values(
            _required_row(
                rows, "Dividend Equivalent Amount per Creation Unit (Note 7)"
            )
        )
        balancing_cash = _currency_values(
            _required_row(rows, "Balancing Cash Amount per Creation Unit (Note 8)")
        )
        estimated_cash = _currency_values(
            _required_row(rows, "Estimated Cash Component per Creation Unit:")
        )

        hkd_nav = _required_currency(nav_per_unit, "HKD")
        rmb_nav = _required_currency(nav_per_unit, "RMB")
        hkd_nav_per_creation = _required_currency(nav_per_creation, "HKD")
        creation_unit = _infer_creation_unit(hkd_nav, hkd_nav_per_creation)
        components = _parse_components(rows)

        raw_fields: Dict[str, Any] = {
            "official_source_page": TRAHK_PCF_SOURCE_PAGE,
            "official_download_url": TRAHK_PCF_DOWNLOAD_URL,
            "valuation_date": valuation_date.isoformat(),
            "basket_effective_date": basket_date.isoformat(),
            "counter_codes": list(TRAHK_COUNTERS),
            "counter_nav": {"HKD": hkd_nav, "RMB": rmb_nav},
            "actual_cash_per_creation_unit": dict(actual_cash),
            "estimated_cash_per_creation_unit": dict(estimated_cash),
            "dividend_equivalent_per_unit": dict(dividend_per_unit),
            "dividend_equivalent_per_creation_unit": dict(
                dividend_per_creation
            ),
            "balancing_cash_per_creation_unit": dict(balancing_cash),
            "primary_market_daily_status": "NOT_DISCLOSED_IN_PUBLIC_PCF",
            "substitution_rules": "NOT_DISCLOSED_PER_COMPONENT",
        }

        return PCFDocument(
            version="TRAHK-{}".format(basket_date.strftime("%Y%m%d")),
            etf_code=TRAHK_HKD_COUNTER,
            security_id_source="103",
            symbol=str(fund_name),
            fund_management_company=str(manager),
            underlying_index="HANG SENG INDEX",
            underlying_security_id_source="OTHER",
            creation_redemption_unit=creation_unit,
            estimate_cash_component=_required_currency(estimated_cash, "HKD"),
            max_cash_ratio=0.0,
            publish=True,
            # The public workbook proves the basket but does not publish a
            # machine-readable daily open/suspended flag.  Keep both false so
            # downstream code cannot accidentally call the PCF executable.
            creation_allowed=False,
            redemption_allowed=False,
            record_num=len(components),
            total_record_num=len(components),
            trading_day=basket_date,
            previous_trading_day=valuation_date,
            cash_component=_required_currency(actual_cash, "HKD"),
            nav_per_creation_unit=hkd_nav_per_creation,
            nav=hkd_nav,
            components=components,
            listing_exchange=Exchange.HKEX.value,
            schema_version="trahk-public-pcf-xlsx",
            parser_version=self.parser_version,
            file_hash=file_hash,
            creation_redemption_mechanism="IN_KIND_WITH_CASH_BALANCE",
            account_requirements=("PARTICIPATING_DEALER_REQUIRED",),
            settlement_rules=(
                "PRIMARY_MARKET_STATUS_MUST_BE_CONFIRMED_SEPARATELY",
                "PUBLIC_PCF_DOES_NOT_DISCLOSE_COMPONENT_SUBSTITUTION_FLAGS",
            ),
            raw_fields=raw_fields,
        )


class HKPCFRepository:
    """Validated local storage for the official latest TraHK PCF workbook."""

    def __init__(
        self,
        root: Union[Path, str],
        download_url: str = TRAHK_PCF_DOWNLOAD_URL,
    ) -> None:
        self.root = Path(root)
        self.download_url = str(download_url)
        self.parser = HKPCFParser()

    def path_for(
        self,
        trading_day: Union[date, str],
        etf_code: str = TRAHK_HKD_COUNTER,
    ) -> Path:
        normalize_trahk_counter(etf_code)
        day = _day_text(trading_day)
        return self.root / day / "pcf_2800_{}.xlsx".format(day)

    def find(
        self,
        trading_day: Union[date, str],
        etf_code: str = TRAHK_HKD_COUNTER,
    ) -> Optional[Path]:
        path = self.path_for(trading_day, etf_code)
        if not path.exists():
            return None
        self.validate(path, trading_day, etf_code)
        return path

    def save(
        self,
        content: bytes,
        trading_day: Union[date, str],
        etf_code: str = TRAHK_HKD_COUNTER,
    ) -> Path:
        document = self.parser.parse_bytes(content)
        self._validate_document(document, trading_day, etf_code)
        destination = self.path_for(trading_day, etf_code)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".xlsx.tmp")
        temporary.write_bytes(content)
        try:
            self.validate(temporary, trading_day, etf_code)
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    def download(
        self,
        trading_day: Union[date, str],
        etf_code: str = TRAHK_HKD_COUNTER,
        timeout: float = 30.0,
    ) -> Path:
        request = Request(
            self.download_url,
            headers={
                "User-Agent": "ETF-Arbitrage-Research/1.0",
                "Accept": (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet,application/octet-stream,*/*"
                ),
                "Referer": TRAHK_PCF_SOURCE_PAGE,
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                content = response.read()
        except Exception as exc:
            raise HKPCFValidationError(
                "Unable to download the official TraHK PCF: {}".format(exc)
            ) from exc
        return self.save(content, trading_day, etf_code)

    def validate(
        self,
        path: Union[Path, str],
        trading_day: Union[date, str],
        etf_code: str = TRAHK_HKD_COUNTER,
    ) -> PCFDocument:
        document = self.parser.parse(path)
        self._validate_document(document, trading_day, etf_code)
        return document

    def latest_local_day(self) -> date:
        for path in sorted(self.root.glob("*/pcf_2800_*.xlsx"), reverse=True):
            try:
                return datetime.strptime(path.parent.name, "%Y%m%d").date()
            except ValueError:
                continue
        return date.today()

    @staticmethod
    def _validate_document(
        document: PCFDocument,
        trading_day: Union[date, str],
        etf_code: str,
    ) -> None:
        requested_counter = normalize_trahk_counter(etf_code)
        expected_day = datetime.strptime(_day_text(trading_day), "%Y%m%d").date()
        counters = tuple(document.raw_fields.get("counter_codes", ()))
        if requested_counter not in counters:
            raise HKPCFValidationError(
                "Official PCF does not contain requested counter {}".format(
                    requested_counter
                )
            )
        if document.trading_day != expected_day:
            raise HKPCFValidationError(
                "Official PCF basket date is {}, requested {}".format(
                    document.trading_day,
                    expected_day,
                )
            )


def _read_first_worksheet(content: bytes) -> List[List[Any]]:
    try:
        with ZipFile(BytesIO(content)) as archive:
            if sum(item.file_size for item in archive.infolist()) > _MAX_UNCOMPRESSED_BYTES:
                raise PCFParseError("TraHK PCF workbook expands beyond the safety limit")
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            relationships = ET.fromstring(
                archive.read("xl/_rels/workbook.xml.rels")
            )
            first_sheet = workbook.find(
                "{{{}}}sheets/{{{}}}sheet".format(_MAIN_NS, _MAIN_NS)
            )
            if first_sheet is None:
                raise PCFParseError("TraHK PCF workbook contains no worksheet")
            relation_id = first_sheet.attrib.get("{{{}}}id".format(_REL_NS))
            targets = {
                rel.attrib.get("Id"): rel.attrib.get("Target")
                for rel in relationships.findall("{{{}}}Relationship".format(_PKG_REL_NS))
            }
            target = targets.get(relation_id)
            if not target:
                raise PCFParseError("TraHK PCF worksheet relationship is missing")
            if target.startswith("/"):
                worksheet_name = target.lstrip("/")
            else:
                worksheet_name = posixpath.normpath(
                    str(PurePosixPath("xl") / target)
                )
            shared_strings = _shared_strings(archive)
            worksheet = ET.fromstring(archive.read(worksheet_name))
    except (BadZipFile, KeyError, ET.ParseError, OSError) as exc:
        raise PCFParseError("TraHK PCF must be a valid XLSX workbook") from exc

    cells: Dict[int, Dict[int, Any]] = {}
    max_column = 0
    for cell in worksheet.findall(".//{{{}}}c".format(_MAIN_NS)):
        reference = cell.attrib.get("r", "")
        match = _CELL_REFERENCE.match(reference)
        if not match:
            continue
        column = _column_number(match.group(1))
        row_number = int(match.group(2))
        value = _cell_value(cell, shared_strings)
        cells.setdefault(row_number, {})[column] = value
        max_column = max(max_column, column)
    if not cells:
        raise PCFParseError("TraHK PCF worksheet contains no values")
    return [
        [cells.get(row, {}).get(column) for column in range(1, max_column + 1)]
        for row in range(1, max(cells) + 1)
    ]


def _shared_strings(archive: ZipFile) -> List[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    return [
        "".join(text.text or "" for text in item.findall(".//{{{}}}t".format(_MAIN_NS)))
        for item in root.findall("{{{}}}si".format(_MAIN_NS))
    ]


def _cell_value(cell: ET.Element, shared_strings: Sequence[str]) -> Any:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(
            text.text or ""
            for text in cell.findall(".//{{{}}}t".format(_MAIN_NS))
        )
    value_element = cell.find("{{{}}}v".format(_MAIN_NS))
    if value_element is None or value_element.text is None:
        return None
    raw = value_element.text
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError) as exc:
            raise PCFParseError("Invalid shared string reference in TraHK PCF") from exc
    if cell_type in {"str", "d"}:
        return raw
    if cell_type == "b":
        return raw == "1"
    try:
        number = float(raw)
    except ValueError:
        return raw
    return int(number) if number.is_integer() else number


def _required_row(rows: Sequence[Sequence[Any]], label: str) -> Sequence[Any]:
    target = _normalise_label(label)
    for row in rows:
        for value in row:
            if isinstance(value, str) and _normalise_label(value) == target:
                return row
    raise PCFParseError("Missing required TraHK PCF row: {}".format(label))


def _next_value(row: Sequence[Any], label_column: int) -> Any:
    for value in row[label_column + 1 :]:
        if value is not None and str(value).strip():
            return value
    raise PCFParseError("TraHK PCF metadata row has no value")


def _counter_codes(row: Sequence[Any]) -> set[str]:
    values = set()
    for value in row[1:]:
        if value is None:
            continue
        try:
            text = str(int(value)) if isinstance(value, (int, float)) else str(value)
        except (TypeError, ValueError):
            continue
        text = text.strip().lstrip("0") or "0"
        if text in TRAHK_COUNTERS:
            values.add(text)
    return values


def _currency_values(row: Sequence[Any]) -> Dict[str, float]:
    values: Dict[str, float] = {}
    for index, value in enumerate(row[:-1]):
        currency = str(value).strip().upper() if value is not None else ""
        if currency not in {"HKD", "RMB", "USD", "CNH"}:
            continue
        candidate = row[index + 1]
        if isinstance(candidate, (int, float)):
            values[currency] = float(candidate)
    if not values:
        raise PCFParseError("TraHK PCF cash/NAV row contains no currency value")
    return values


def _required_currency(values: Mapping[str, float], currency: str) -> float:
    try:
        return float(values[currency])
    except KeyError as exc:
        raise PCFParseError(
            "TraHK PCF row is missing {} value".format(currency)
        ) from exc


def _first_date(row: Sequence[Any], start: int = 0) -> date:
    for value in row[start:]:
        if value is None or value == "":
            continue
        try:
            return _as_date(value)
        except (TypeError, ValueError):
            continue
    raise PCFParseError("TraHK PCF date row contains no valid date")


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        return (_EXCEL_EPOCH + timedelta(days=float(value))).date()
    text = str(value).strip()
    for pattern in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise ValueError("not a date")


def _infer_creation_unit(nav: float, nav_per_creation_unit: float) -> int:
    if nav <= 0 or nav_per_creation_unit <= 0:
        raise PCFParseError("TraHK NAV values must be positive")
    inferred = nav_per_creation_unit / nav
    rounded = int(round(inferred))
    if rounded <= 0 or abs(inferred - rounded) > 1e-5:
        raise PCFParseError("Unable to infer an integer TraHK creation unit")
    return rounded


def _parse_components(rows: Sequence[Sequence[Any]]) -> tuple[PCFComponent, ...]:
    basket_label = _normalise_label("Index Basket Shares per Creation Unit as of")
    start = None
    for index, row in enumerate(rows):
        if any(
            isinstance(value, str) and _normalise_label(value) == basket_label
            for value in row
        ):
            start = index + 1
            break
    if start is None:
        raise PCFParseError("TraHK PCF basket section is missing")

    header_index = None
    code_column = name_column = quantity_column = None
    for index in range(start, len(rows)):
        labels = {
            _normalise_label(value): column
            for column, value in enumerate(rows[index])
            if isinstance(value, str) and value.strip()
        }
        if {"stock code", "stock name", "quantity"}.issubset(labels):
            header_index = index
            code_column = labels["stock code"]
            name_column = labels["stock name"]
            quantity_column = labels["quantity"]
            break
    if header_index is None:
        raise PCFParseError("TraHK PCF basket column headers are missing")
    assert code_column is not None
    assert name_column is not None
    assert quantity_column is not None

    components: List[PCFComponent] = []
    for row in rows[header_index + 1 :]:
        first_text = _normalise_label(row[0]) if row and row[0] is not None else ""
        if first_text.startswith("estimated cash component") or first_text == "- end -":
            break
        if max(code_column, name_column, quantity_column) >= len(row):
            continue
        raw_code = row[code_column]
        raw_name = row[name_column]
        raw_quantity = row[quantity_column]
        if raw_code is None or not str(raw_code).strip():
            continue
        code = _hk_stock_code(raw_code)
        name = str(raw_name or "").strip()
        if not name:
            raise PCFParseError("TraHK PCF component {} has no name".format(code))
        try:
            quantity = float(raw_quantity)
        except (TypeError, ValueError) as exc:
            raise PCFParseError(
                "TraHK PCF component {} has invalid quantity".format(code)
            ) from exc
        if quantity <= 0 or abs(quantity - round(quantity)) > 1e-9:
            raise PCFParseError(
                "TraHK PCF component {} quantity must be a positive integer".format(
                    code
                )
            )
        components.append(
            PCFComponent(
                stock_code=code,
                security_id_source="103",
                symbol=name,
                component_share=quantity,
                substitute_flag=SubstituteFlag.PROHIBITED,
                premium_ratio=0.0,
                creation_cash_substitute=0.0,
                redemption_cash_substitute=0.0,
                exchange=Exchange.HKEX.value,
                raw_fields={
                    "source": "TRAHK_PUBLIC_PCF",
                    "substitution_rule": "NOT_DISCLOSED",
                },
            )
        )
    if not components:
        raise PCFParseError("TraHK PCF basket contains no components")
    identities = [component.instrument_id for component in components]
    if len(identities) != len(set(identities)):
        raise PCFParseError("TraHK PCF basket contains duplicate securities")
    return tuple(components)


def _hk_stock_code(value: Any) -> str:
    if isinstance(value, (int, float)):
        if float(value).is_integer():
            return "{:04d}".format(int(value))
        raise PCFParseError("Invalid Hong Kong stock code in TraHK PCF")
    text = str(value or "").strip().upper()
    if text.endswith(".HK"):
        text = text[:-3]
    if not text.isdigit() or len(text) > 5:
        raise PCFParseError("Invalid Hong Kong stock code in TraHK PCF: {}".format(text))
    return text.zfill(4)


def _column_number(letters: str) -> int:
    value = 0
    for character in letters:
        value = value * 26 + (ord(character) - ord("A") + 1)
    return value


def _normalise_label(value: Any) -> str:
    return " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split()).casefold()


def _day_text(value: Union[date, str]) -> str:
    text = value.strftime("%Y%m%d") if isinstance(value, date) else str(value)
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError as exc:
        raise ValueError("trading_day must use YYYYMMDD format") from exc
    return text
