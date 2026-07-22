"""Validated local storage and optional HTTP download for SZSE PCF files."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO
import json
import os
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Union
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
from zipfile import BadZipFile, ZipFile

from .pcf import PCFDocument, PCFParseError, SZSEPCFParser


@dataclass(frozen=True)
class SZSEETFProfile:
    etf_code: str
    name: str
    manager: str
    tracking_index: str


SZSE_ETFS: Mapping[str, SZSEETFProfile] = {
    "159915": SZSEETFProfile("159915", "创业板ETF易方达", "易方达基金", "399006 创业板指"),
    "159901": SZSEETFProfile("159901", "深证100ETF易方达", "易方达基金", "399330 深证100"),
    "159949": SZSEETFProfile("159949", "创业板50ETF华安", "华安基金", "399673 创业板50"),
    "159903": SZSEETFProfile("159903", "深成ETF南方", "南方基金", "399001 深证成指"),
}


class PCFValidationError(ValueError):
    """Raised when a downloaded or uploaded PCF does not match the request."""


class PCFRepository:
    """Store PCFs under ``data/pcf/YYYYMMDD`` after strict validation."""

    def __init__(self, root: Union[Path, str]) -> None:
        self.root = Path(root)
        self.parser = SZSEPCFParser()

    def path_for(self, etf_code: str, trading_day: Union[date, str]) -> Path:
        day = self._day_text(trading_day)
        return self.root / day / "pcf_{}_{}.xml".format(etf_code, day)

    def find(self, etf_code: str, trading_day: Union[date, str]) -> Optional[Path]:
        expected = self.path_for(etf_code, trading_day)
        if expected.exists():
            self.validate(expected, etf_code, trading_day)
            return expected
        day = self._day_text(trading_day)
        folder = self.root / day
        if not folder.exists():
            return None
        for candidate in sorted(folder.glob("*.xml")):
            try:
                self.validate(candidate, etf_code, trading_day)
            except (PCFParseError, PCFValidationError):
                continue
            return candidate
        return None

    def save(self, content: bytes, etf_code: str, trading_day: Union[date, str]) -> Path:
        xml = self._extract_xml(content, etf_code)
        destination = self.path_for(etf_code, trading_day)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".xml.tmp")
        temporary.write_bytes(xml)
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
        url = self.render_url(url_template, etf_code, trading_day)
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
            return [url]

        query = parse_qs(parsed.query)
        source_path = query.get("path", [""])[0]
        filenames = query.get("filename", [""])[0].split(";")
        if not source_path.startswith("/files/text/"):
            raise PCFValidationError("深交所下载页面缺少有效的PCF文件路径")

        day = PCFRepository._day_text(trading_day)
        expected_name = "pcf_{}_{}".format(etf_code, day)
        ordered_names = [expected_name] + [name for name in filenames if name]
        unique_names = list(dict.fromkeys(ordered_names))
        base = "https://reportdocs.static.szse.cn{}".format(source_path.rstrip("/"))
        candidates = []
        for name in unique_names:
            suffix = Path(name).suffix.lower()
            if suffix in {".xml", ".txt"}:
                candidates.append("{}/{}".format(base, name))
            else:
                candidates.extend(
                    ["{}/{}.xml".format(base, name), "{}/{}.txt".format(base, name)]
                )
        return candidates

    def validate(
        self,
        path: Union[Path, str],
        etf_code: str,
        trading_day: Union[date, str],
    ) -> PCFDocument:
        document = self.parser.parse(path)
        expected_day = datetime.strptime(self._day_text(trading_day), "%Y%m%d").date()
        if document.etf_code != etf_code:
            raise PCFValidationError(
                "PCF证券代码为{}，与所选{}不一致".format(document.etf_code, etf_code)
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
    for code in SZSE_ETFS:
        environment_value = os.getenv("ETF_PCF_URL_{}".format(code), "").strip()
        if environment_value:
            values[code] = environment_value
    return values
