"""Validated local storage and optional HTTP download for SZSE PCF files."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO
import json
import os
from pathlib import Path
import re
from typing import Dict, Iterable, List, Mapping, Optional, Union
from urllib.parse import parse_qs, urlparse, urlunparse
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

from etf_arbitrage.domain import Exchange, infer_etf_exchange

from .pcf import PCFDocument, PCFParseError, SZSEPCFParser
from .sse_pcf import SSEPCFFetcher, SSEPCFParser


@dataclass(frozen=True)
class ETFProfile:
    etf_code: str
    name: str
    manager: str
    tracking_index: str
    exchange: Exchange = Exchange.SZSE


SZSEETFProfile = ETFProfile

ETF_PROFILES: Mapping[str, ETFProfile] = {
    "159915": ETFProfile("159915", "创业板ETF易方达", "易方达基金", "399006 创业板指"),
    "159901": ETFProfile("159901", "深证100ETF易方达", "易方达基金", "399330 深证100"),
    "159949": ETFProfile("159949", "创业板50ETF华安", "华安基金", "399673 创业板50"),
    "159903": ETFProfile("159903", "深成ETF南方", "南方基金", "399001 深证成指"),
    "159919": ETFProfile("159919", "沪深300ETF嘉实", "嘉实基金", "000300 沪深300"),
    "159920": ETFProfile("159920", "恒生ETF华夏", "华夏基金", "HSI 恒生指数"),
    "510300": ETFProfile(
        "510300",
        "沪深300ETF华泰柏瑞",
        "华泰柏瑞基金",
        "000300 沪深300",
        Exchange.SSE,
    ),
    "510050": ETFProfile(
        "510050",
        "上证50ETF华夏",
        "华夏基金",
        "000016 上证50",
        Exchange.SSE,
    ),
    "510500": ETFProfile(
        "510500",
        "中证500ETF南方",
        "南方基金",
        "000905 中证500",
        Exchange.SSE,
    ),
    "588000": ETFProfile(
        "588000",
        "科创50ETF华夏",
        "华夏基金",
        "000688 科创50",
        Exchange.SSE,
    ),
    "513660": ETFProfile(
        "513660",
        "恒生ETF",
        "华夏基金",
        "恒生指数（估值汇率调整）",
        Exchange.SSE,
    ),
    "513600": ETFProfile(
        "513600",
        "恒生指数ETF",
        "南方基金",
        "恒生指数（估值汇率调整）",
        Exchange.SSE,
    ),
}

SZSE_ETFS: Mapping[str, ETFProfile] = {
    code: profile
    for code, profile in ETF_PROFILES.items()
    if profile.exchange == Exchange.SZSE
}


def extract_etf_code(value: str) -> str:
    """Extract a six-digit ETF code from a code, suffix, or search label."""

    text = str(value).strip()
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    if match:
        return match.group(1)
    lowered = text.casefold()
    exact = [
        code
        for code, profile in ETF_PROFILES.items()
        if lowered in {profile.name.casefold(), profile.etf_code.casefold()}
    ]
    if len(exact) == 1:
        return exact[0]
    raise ValueError("请输入6位ETF代码，或从搜索建议中选择一只ETF")


def etf_profile(etf_code: str) -> ETFProfile:
    """Return catalog metadata or a safe dynamic profile for an unknown ETF."""

    code = extract_etf_code(etf_code)
    profile = ETF_PROFILES.get(code)
    if profile is not None:
        return profile
    return ETFProfile(
        etf_code=code,
        name=code,
        manager="",
        tracking_index="",
        exchange=infer_etf_exchange(code),
    )


def format_etf_search_option(etf_code: str) -> str:
    profile = etf_profile(etf_code)
    suffix = {
        Exchange.SSE: "SH",
        Exchange.SZSE: "SZ",
        Exchange.BSE: "BJ",
        Exchange.HKEX: "HK",
    }.get(profile.exchange, profile.exchange.value)
    return "{} {} [{}]".format(profile.etf_code, profile.name, suffix)


def etf_search_options(extra_codes: Iterable[str] = ()) -> List[str]:
    codes = list(ETF_PROFILES)
    for value in extra_codes:
        try:
            code = extract_etf_code(value)
        except ValueError:
            continue
        if code not in codes:
            codes.append(code)
    return [format_etf_search_option(code) for code in codes]


SZSE_REPORT_DOCUMENT_HOSTS = (
    "reportdocs.static.szse.cn",
    "reportdocs.static.sse.org.cn",
)
SZSE_DEFAULT_PCF_URL_TEMPLATE = (
    "https://reportdocs.static.szse.cn/files/text/ETFDown/"
    "pcf_{etf_code}_{trade_date}.xml"
)


class PCFValidationError(ValueError):
    """Raised when a downloaded or uploaded PCF does not match the request."""


class PCFRepository:
    """Store PCFs under ``data/pcf/YYYYMMDD`` after strict validation."""

    def __init__(self, root: Union[Path, str]) -> None:
        self.root = Path(root)
        self.szse_parser = SZSEPCFParser()
        self.sse_parser = SSEPCFParser()
        self.sse_fetcher = SSEPCFFetcher()

    def path_for(self, etf_code: str, trading_day: Union[date, str]) -> Path:
        day = self._day_text(trading_day)
        code = extract_etf_code(etf_code)
        extension = (
            ".json" if infer_etf_exchange(code) == Exchange.SSE else ".xml"
        )
        return self.root / day / "pcf_{}_{}{}".format(code, day, extension)

    def _path_for_extension(
        self,
        etf_code: str,
        trading_day: Union[date, str],
        extension: str,
    ) -> Path:
        day = self._day_text(trading_day)
        code = extract_etf_code(etf_code)
        return self.root / day / "pcf_{}_{}{}".format(
            code,
            day,
            extension,
        )

    def find(self, etf_code: str, trading_day: Union[date, str]) -> Optional[Path]:
        expected = self.path_for(etf_code, trading_day)
        if expected.exists():
            self.validate(expected, etf_code, trading_day)
            return expected
        day = self._day_text(trading_day)
        folder = self.root / day
        if not folder.exists():
            return None
        for candidate in sorted(
            [*folder.glob("*.xml"), *folder.glob("*.json")]
        ):
            try:
                self.validate(candidate, etf_code, trading_day)
            except (PCFParseError, PCFValidationError):
                continue
            return candidate
        return None

    def save(self, content: bytes, etf_code: str, trading_day: Union[date, str]) -> Path:
        code = extract_etf_code(etf_code)
        exchange = infer_etf_exchange(code)
        if exchange == Exchange.SSE:
            payload = self._extract_sse_payload(content)
            extension = ".json" if payload.lstrip().startswith(b"{") else ".xml"
            destination = self._path_for_extension(
                etf_code,
                trading_day,
                extension,
            )
        else:
            payload = self._extract_xml(content, code)
            destination = self.path_for(etf_code, trading_day)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_bytes(payload)
        try:
            self.validate(temporary, etf_code, trading_day)
            temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    def download(
        self,
        url_template: str,
        etf_code: str,
        trading_day: Union[date, str],
        timeout: float = 20.0,
    ) -> Path:
        code = extract_etf_code(etf_code)
        exchange = infer_etf_exchange(code)
        if exchange == Exchange.SSE and not url_template.strip():
            content = self.sse_fetcher.fetch_bytes(
                code,
                trading_day=trading_day,
                timeout=timeout,
            )
            return self.save(content, code, trading_day)
        if exchange == Exchange.SZSE and not url_template.strip():
            url_template = SZSE_DEFAULT_PCF_URL_TEMPLATE
        url = self.render_url(url_template, code, trading_day)
        errors = []
        for candidate in self.download_candidates(url, etf_code, trading_day):
            request = Request(
                candidate,
                headers={
                    "User-Agent": "ETF-Arbitrage-Research/1.0",
                    "Referer": "https://www.szse.cn/",
                },
            )
            try:
                with urlopen(request, timeout=timeout) as response:
                    content = response.read()
                if not content:
                    raise PCFValidationError("下载结果为空")
                return self.save(content, etf_code, trading_day)
            except Exception as exc:
                errors.append("{} -> {}".format(candidate, exc))
        raise PCFValidationError(
            "未能从下载地址获取有效PCF：{}".format(" | ".join(errors))
        )

    @staticmethod
    def download_candidates(
        url: str,
        etf_code: str,
        trading_day: Union[date, str],
    ) -> List[str]:
        """Resolve an SZSE download landing page into direct report-document URLs."""
        parsed = urlparse(url)
        if not (
            parsed.hostname
            and parsed.hostname.lower().endswith("szse.cn")
            and parsed.path.endswith("/eft_download_new.html")
        ):
            return PCFRepository._report_document_candidates(url)

        query = parse_qs(parsed.query)
        source_path = query.get("path", [""])[0]
        filenames = query.get("filename", [""])[0].split(";")
        if not source_path.startswith("/files/text/"):
            raise PCFValidationError("深交所下载页面缺少有效的PCF文件路径")

        day = PCFRepository._day_text(trading_day)
        expected_name = "pcf_{}_{}".format(etf_code, day)
        ordered_names = [expected_name] + [name for name in filenames if name]
        unique_names = list(dict.fromkeys(ordered_names))
        bases = [
            "https://{}{}".format(host, source_path.rstrip("/"))
            for host in SZSE_REPORT_DOCUMENT_HOSTS
        ]
        candidates = []
        for name in unique_names:
            suffix = Path(name).suffix.lower()
            if suffix in {".xml", ".txt"}:
                candidates.extend("{}/{}".format(base, name) for base in bases)
            else:
                for extension in ("xml", "txt"):
                    candidates.extend(
                        "{}/{}.{}".format(base, name, extension) for base in bases
                    )
        return list(dict.fromkeys(candidates))

    @staticmethod
    def _report_document_candidates(url: str) -> List[str]:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower()
        if hostname not in SZSE_REPORT_DOCUMENT_HOSTS:
            return [url]
        ordered_hosts = [hostname] + [
            host for host in SZSE_REPORT_DOCUMENT_HOSTS if host != hostname
        ]
        return [
            urlunparse(parsed._replace(netloc=host)) for host in ordered_hosts
        ]

    def validate(
        self,
        path: Union[Path, str],
        etf_code: str,
        trading_day: Union[date, str],
    ) -> PCFDocument:
        source = Path(path)
        prefix = source.read_bytes().lstrip()[:1]
        parser = (
            self.sse_parser
            if infer_etf_exchange(extract_etf_code(etf_code)) == Exchange.SSE
            or source.suffix.lower() == ".json"
            or prefix == b"{"
            else self.szse_parser
        )
        document = parser.parse(source)
        expected_day = datetime.strptime(self._day_text(trading_day), "%Y%m%d").date()
        expected_code = extract_etf_code(etf_code)
        if document.etf_code != expected_code:
            raise PCFValidationError(
                "PCF证券代码为{}，与所选{}不一致".format(
                    document.etf_code,
                    expected_code,
                )
            )
        if document.trading_day != expected_day:
            raise PCFValidationError(
                "PCF交易日为{}，与所选{}不一致".format(
                    document.trading_day, expected_day
                )
            )
        return document

    @staticmethod
    def render_url(
        url_template: str,
        etf_code: str,
        trading_day: Union[date, str],
    ) -> str:
        day = PCFRepository._day_text(trading_day)
        if not url_template.strip():
            raise PCFValidationError("尚未配置该ETF的PCF直接下载地址")
        return url_template.strip().format(
            etf_code=etf_code,
            trade_date=day,
            trade_date_dash="{}-{}-{}".format(day[:4], day[4:6], day[6:]),
        )

    @staticmethod
    def _extract_xml(content: bytes, etf_code: str) -> bytes:
        if content[:2] != b"PK":
            prefix = content.lstrip()[:100].lower()
            if prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html"):
                raise PCFValidationError(
                    "下载地址返回的是网页，不是PCF文件；请使用深交所PCF下载页或XML直链"
                )
            return content
        try:
            with ZipFile(BytesIO(content)) as archive:
                names = [name for name in archive.namelist() if name.lower().endswith(".xml")]
                preferred = [name for name in names if etf_code in Path(name).name]
                candidates = preferred or names
                if len(candidates) != 1:
                    raise PCFValidationError("压缩包内无法唯一识别目标PCF XML")
                return archive.read(candidates[0])
        except BadZipFile as exc:
            raise PCFValidationError("PCF压缩包损坏") from exc

    @staticmethod
    def _extract_sse_payload(content: bytes) -> bytes:
        if content[:2] == b"PK":
            try:
                with ZipFile(BytesIO(content)) as archive:
                    names = [
                        name
                        for name in archive.namelist()
                        if name.lower().endswith((".json", ".xml"))
                    ]
                    if len(names) != 1:
                        raise PCFValidationError(
                            "压缩包内无法唯一识别沪市PCF JSON/XML"
                        )
                    content = archive.read(names[0])
            except BadZipFile as exc:
                raise PCFValidationError("PCF压缩包损坏") from exc
        prefix = content.lstrip()[:100].lower()
        if prefix.startswith(b"<!doctype html") or prefix.startswith(b"<html"):
            raise PCFValidationError(
                "下载地址返回的是网页，不是沪市PCF JSON/XML文件"
            )
        if prefix.startswith(b"<"):
            return content
        try:
            payload = json.loads(content.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PCFValidationError(
                "沪市PCF需要上交所官方查询JSON、管理人官方XML或其ZIP文件"
            ) from exc
        if not isinstance(payload, dict):
            raise PCFValidationError("沪市PCF JSON根节点必须是对象")
        return json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")

    @staticmethod
    def _day_text(value: Union[date, str]) -> str:
        text = value.strftime("%Y%m%d") if isinstance(value, date) else str(value)
        try:
            datetime.strptime(text, "%Y%m%d")
        except ValueError as exc:
            raise ValueError("trading_day must use YYYYMMDD format") from exc
        return text


def load_pcf_source_templates(path: Union[Path, str]) -> Dict[str, str]:
    """Load public URL templates, with environment variables taking precedence."""
    source_path = Path(path)
    values: Dict[str, str] = {}
    if source_path.exists():
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("PCF source configuration must be a JSON object")
        values.update({str(code): str(url) for code, url in payload.items() if url})
    for code in ETF_PROFILES:
        environment_value = os.getenv("ETF_PCF_URL_{}".format(code), "").strip()
        if environment_value:
            values[code] = environment_value
    return values
