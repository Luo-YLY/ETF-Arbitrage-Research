"""Executable-arbitrage validation and metadata for parsed PCFs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
from pathlib import Path
from typing import Optional, Union

from .pcf import PCFDocument, SubstituteFlag, SZSEPCFParser


@dataclass(frozen=True)
class PCFValidationReport:
    valid: bool
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    file_hash: str
    file_size: int
    modified_time: datetime
    component_count: int
    prohibited_count: int
    optional_count: int
    mandatory_count: int


def validate_executable_pcf(
    path: Union[str, Path],
    expected_code: Optional[str] = None,
    expected_date: Optional[date] = None,
) -> tuple[PCFDocument, PCFValidationReport]:
    source = Path(path)
    content = source.read_bytes()
    document = SZSEPCFParser().parse(source)
    errors = []
    warnings = []
    if expected_code and document.etf_code != expected_code:
        errors.append("ETF_CODE_MISMATCH")
    if expected_date and document.trading_day != expected_date:
        errors.append("TRADING_DATE_MISMATCH")
    if document.creation_redemption_unit <= 0:
        errors.append("INVALID_CREATION_REDEMPTION_UNIT")
    if document.record_num != len(document.components):
        errors.append("COMPONENT_COUNT_MISMATCH")
    if not document.publish:
        warnings.append("PCF_NOT_PUBLISHED")
    if not document.creation_allowed:
        warnings.append("CREATION_CLOSED")
    if not document.redemption_allowed:
        warnings.append("REDEMPTION_CLOSED")
    if document.estimate_cash_component != document.estimate_cash_component:
        errors.append("INVALID_ESTIMATE_CASH_COMPONENT")
    if any(
        item.substitute_flag == SubstituteFlag.MANDATORY
        and item.creation_cash_substitute == 0
        and item.redemption_cash_substitute == 0
        for item in document.components
    ):
        warnings.append("MANDATORY_SUBSTITUTION_WITH_ZERO_CASH")
    counts = {
        flag: sum(item.substitute_flag == flag for item in document.components)
        for flag in SubstituteFlag
    }
    stat = source.stat()
    report = PCFValidationReport(
        valid=not errors,
        errors=tuple(errors),
        warnings=tuple(warnings),
        file_hash=hashlib.sha256(content).hexdigest(),
        file_size=len(content),
        modified_time=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        component_count=len(document.components),
        prohibited_count=counts[SubstituteFlag.PROHIBITED],
        optional_count=counts[SubstituteFlag.ALLOWED],
        mandatory_count=counts[SubstituteFlag.MANDATORY],
    )
    return document, report
