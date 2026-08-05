"""Data-quality gates and kill-switch decisions for executable pricing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, Optional
from zoneinfo import ZoneInfo

from etf_arbitrage.data.pcf import PCFDocument, SubstituteFlag
from etf_arbitrage.domain import Exchange
from etf_arbitrage.executable_config import DataQualityConfig

from .models import DataQualityStatus, MarketSnapshot, OrderBook, TradingStatus


CHINA_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class DataQualityReport:
    status: DataQualityStatus
    kill_switch: bool
    new_trades_enabled: bool
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    etf_quote_age_ms: float
    max_component_quote_age_ms: float
    max_cross_section_skew_ms: float
    coverage: float
    missing_weight: float
    stale_weight: float
    suspended_weight: float
    limit_up_no_ask_weight: float
    limit_down_no_bid_weight: float
    iopv_error_bps: float

    def to_dict(self) -> Dict[str, object]:
        return {
            "status": self.status.value,
            "kill_switch": self.kill_switch,
            "new_trades_enabled": self.new_trades_enabled,
            "blockers": ",".join(self.blockers),
            "warnings": ",".join(self.warnings),
            "etf_quote_age_ms": self.etf_quote_age_ms,
            "max_component_quote_age_ms": self.max_component_quote_age_ms,
            "max_cross_section_skew_ms": self.max_cross_section_skew_ms,
            "coverage": self.coverage,
            "missing_weight": self.missing_weight,
            "stale_weight": self.stale_weight,
            "suspended_weight": self.suspended_weight,
            "limit_up_no_ask_weight": self.limit_up_no_ask_weight,
            "limit_down_no_bid_weight": self.limit_down_no_bid_weight,
            "iopv_error_bps": self.iopv_error_bps,
        }


class DataQualityChecker:
    def __init__(self, config: DataQualityConfig = DataQualityConfig()) -> None:
        self.config = config

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        pcf: PCFDocument,
        internal_iopv: Optional[float] = None,
    ) -> DataQualityReport:
        blockers = []
        warnings = []
        critical = []
        snapshot_day = self._china_date(snapshot.snapshot_timestamp)
        if snapshot_day != pcf.trading_day:
            critical.append("PCF_DATE_MISMATCH")
        if snapshot.decode_error:
            critical.append("DECODE_ERROR")
        if snapshot.sequence_gap:
            critical.append("SEQUENCE_GAP")

        etf = snapshot.etf_order_book
        etf_age = self._age_ms(snapshot.snapshot_timestamp, etf.exchange_timestamp)
        if etf_age > self.config.max_quote_age_ms:
            blockers.append("STALE_ETF_QUOTE")
        self._check_book(etf, critical, "ETF")
        if not etf.has_two_sided_book:
            blockers.append("MISSING_ETF_TWO_SIDED_BOOK")

        active = [
            item
            for item in pcf.components
            if item.component_share > 0
            and not item.substitute_flag.requires_cash_substitution
        ]
        total_quantity = sum(item.component_share for item in active) or 1.0
        missing = stale = suspended = limit_up = limit_down = 0.0
        max_age = 0.0
        max_skew = 0.0
        for component in active:
            weight = component.component_share / total_quantity
            book = snapshot.component_order_books.get(component.stock_code)
            if book is None:
                missing += weight
                continue
            age = self._age_ms(snapshot.snapshot_timestamp, book.exchange_timestamp)
            skew = abs((etf.exchange_timestamp - book.exchange_timestamp).total_seconds()) * 1_000.0
            max_age = max(max_age, age)
            max_skew = max(max_skew, skew)
            if age > self.config.max_quote_age_ms:
                stale += weight
            if book.trading_status in {TradingStatus.SUSPENDED, TradingStatus.HALTED}:
                suspended += weight
            if book.trading_status == TradingStatus.LIMIT_UP and not book.asks:
                limit_up += weight
            if book.trading_status == TradingStatus.LIMIT_DOWN and not book.bids:
                limit_down += weight
            self._check_book(book, critical, component.stock_code)

        if any(
            component.instrument_id.exchange == Exchange.HKEX
            for component in active
        ):
            fx_quote = snapshot.hkd_cny_quote
            if fx_quote is None:
                blockers.append("MISSING_HKD_CNY_QUOTE")
            elif fx_quote.exchange_timestamp is None:
                blockers.append("MISSING_HKD_CNY_TIMESTAMP")
            else:
                fx_age = self._age_ms(
                    snapshot.snapshot_timestamp, fx_quote.exchange_timestamp
                )
                fx_skew = abs(
                    (
                        etf.exchange_timestamp - fx_quote.exchange_timestamp
                    ).total_seconds()
                ) * 1_000.0
                max_age = max(max_age, fx_age)
                max_skew = max(max_skew, fx_skew)
                if fx_age > self.config.max_quote_age_ms:
                    blockers.append("STALE_HKD_CNY_QUOTE")
                if fx_skew > self.config.max_cross_section_skew_ms:
                    blockers.append("HKD_CNY_TIME_SKEW")
            if fx_quote is not None and fx_quote.receive_timestamp is None:
                blockers.append("MISSING_HKD_CNY_RECEIVE_TIMESTAMP")

        if missing > self.config.maximum_missing_weight:
            blockers.append("MISSING_COMPONENT_QUOTES")
        if stale > self.config.maximum_stale_weight:
            blockers.append("STALE_COMPONENT_QUOTES")
        if max_skew > self.config.max_cross_section_skew_ms:
            blockers.append("CROSS_SECTION_TIME_SKEW")
        if suspended > self.config.maximum_suspended_weight:
            blockers.append("SUSPENDED_WEIGHT")
        if limit_up > self.config.maximum_limit_up_weight:
            blockers.append("LIMIT_UP_NO_ASK_WEIGHT")
        if limit_down > self.config.maximum_limit_down_weight:
            blockers.append("LIMIT_DOWN_NO_BID_WEIGHT")

        effective_internal = internal_iopv if internal_iopv is not None else snapshot.internal_iopv
        iopv_error = 0.0
        if snapshot.official_iopv and effective_internal and snapshot.official_iopv > 0:
            iopv_error = abs(effective_internal - snapshot.official_iopv) / snapshot.official_iopv * 10_000.0
            if iopv_error > self.config.maximum_iopv_error_bps:
                blockers.append("IOPV_DIVERGENCE")
        elif snapshot.official_iopv is None:
            warnings.append("OFFICIAL_IOPV_MISSING")

        unique_critical = tuple(dict.fromkeys(critical))
        unique_blockers = tuple(dict.fromkeys(blockers))
        kill = bool(unique_critical) and self.config.kill_switch_enabled
        all_blockers = unique_critical + unique_blockers
        status = (
            DataQualityStatus.INVALID
            if unique_critical
            else DataQualityStatus.DEGRADED
            if unique_blockers
            else DataQualityStatus.GOOD
        )
        return DataQualityReport(
            status=status,
            kill_switch=kill,
            new_trades_enabled=not all_blockers,
            blockers=all_blockers,
            warnings=tuple(warnings),
            etf_quote_age_ms=etf_age,
            max_component_quote_age_ms=max_age,
            max_cross_section_skew_ms=max_skew,
            coverage=max(0.0, 1.0 - missing),
            missing_weight=missing,
            stale_weight=stale,
            suspended_weight=suspended,
            limit_up_no_ask_weight=limit_up,
            limit_down_no_bid_weight=limit_down,
            iopv_error_bps=iopv_error,
        )

    @staticmethod
    def _check_book(book: OrderBook, critical: list[str], label: str) -> None:
        levels: Iterable = tuple(book.bids) + tuple(book.asks)
        if any(level.price <= 0 or level.quantity < 0 for level in levels):
            critical.append("ILLEGAL_BOOK_LEVEL:{}".format(label))
        if book.best_bid is not None and book.best_ask is not None and book.best_bid > book.best_ask:
            critical.append("CROSSED_BOOK:{}".format(label))

    @staticmethod
    def _age_ms(now: datetime, then: datetime) -> float:
        return max(0.0, (now - then).total_seconds() * 1_000.0)

    @staticmethod
    def _china_date(value: datetime):
        if value.tzinfo is None:
            return value.date()
        return value.astimezone(CHINA_TZ).date()
