"""Direction-aware quality gates for executable ETF creation/redemption."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Dict, Iterable, Optional
from zoneinfo import ZoneInfo

from etf_arbitrage.data.pcf import PCFComponent, PCFDocument
from etf_arbitrage.domain import Exchange
from etf_arbitrage.executable_config import DataQualityConfig

from .models import (
    DataQualityStatus,
    FeedHealthStatus,
    InstrumentState,
    MarketPhase,
    MarketSnapshot,
    OrderBook,
    TradingStatus,
)


CHINA_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class DataQualityReport:
    status: DataQualityStatus
    kill_switch: bool
    new_trades_enabled: bool
    creation_enabled: bool
    redemption_enabled: bool
    blockers: tuple[str, ...]
    creation_blockers: tuple[str, ...]
    redemption_blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    market_phase: MarketPhase
    feed_health: FeedHealthStatus
    source_watermark_age_ms: float
    etf_quote_age_ms: float
    max_component_quote_age_ms: float
    max_cross_section_skew_ms: float
    coverage: float
    missing_weight: float
    creation_missing_weight: float
    redemption_missing_weight: float
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
            "creation_enabled": self.creation_enabled,
            "redemption_enabled": self.redemption_enabled,
            "blockers": ",".join(self.blockers),
            "creation_blockers": ",".join(self.creation_blockers),
            "redemption_blockers": ",".join(self.redemption_blockers),
            "warnings": ",".join(self.warnings),
            "market_phase": self.market_phase.value,
            "feed_health": self.feed_health.value,
            "source_watermark_age_ms": self.source_watermark_age_ms,
            "etf_quote_age_ms": self.etf_quote_age_ms,
            "max_component_quote_age_ms": self.max_component_quote_age_ms,
            "max_cross_section_skew_ms": self.max_cross_section_skew_ms,
            "coverage": self.coverage,
            "missing_weight": self.missing_weight,
            "creation_missing_weight": self.creation_missing_weight,
            "redemption_missing_weight": self.redemption_missing_weight,
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
        common: list[str] = []
        creation: list[str] = []
        redemption: list[str] = []
        warnings: list[str] = []
        critical: list[str] = []

        if self._china_date(snapshot.snapshot_timestamp) != pcf.trading_day:
            critical.append("PCF_DATE_MISMATCH")
        if snapshot.decode_error:
            critical.append("DECODE_ERROR")
        if snapshot.sequence_gap:
            critical.append("SEQUENCE_GAP")
        if snapshot.feed_health in {
            FeedHealthStatus.SOURCE_STALE,
            FeedHealthStatus.DISCONNECTED,
        }:
            critical.append(snapshot.feed_health.value)
        elif snapshot.feed_health == FeedHealthStatus.PARTIAL_MISSING:
            warnings.append("PARTIAL_SOURCE_PAYLOAD")

        if snapshot.market_phase not in {
            MarketPhase.UNKNOWN,
            MarketPhase.CONTINUOUS,
        }:
            common.append(
                "NON_CONTINUOUS_MARKET_PHASE:{}".format(
                    snapshot.market_phase.value
                )
            )
        elif (
            snapshot.market_phase == MarketPhase.CONTINUOUS
            and snapshot.source_watermark_age_ms
            > self.config.max_source_watermark_age_ms
        ):
            critical.append("SOURCE_WATERMARK_STALE")

        etf = snapshot.etf_order_book
        etf_age = self._age_ms(snapshot.snapshot_timestamp, etf.exchange_timestamp)
        self._check_book(etf, critical, "ETF")
        if not etf.bids:
            creation.append("CREATION_MISSING_ETF_BID")
        if not etf.asks:
            redemption.append("REDEMPTION_MISSING_ETF_ASK")
        if etf_age > self.config.max_quote_age_ms:
            warnings.append("ETF_QUOTE_INACTIVITY")

        active = [
            item
            for item in pcf.components
            if item.component_share > 0
            and not item.substitute_flag.requires_cash_substitution
        ]
        weights, missing_references = self._notional_weights(
            active, snapshot.component_order_books
        )
        if missing_references:
            warnings.append("MISSING_COMPONENT_REFERENCE_PRICE")

        creation_missing = redemption_missing = stale = 0.0
        suspended = limit_up = limit_down = 0.0
        max_age = max_skew = 0.0
        any_one_sided = False
        for component in active:
            weight = weights.get(component.stock_code, 0.0)
            book = snapshot.component_order_books.get(component.stock_code)
            if book is None:
                creation_missing += weight
                redemption_missing += weight
                continue

            age = self._age_ms(snapshot.snapshot_timestamp, book.exchange_timestamp)
            skew = abs(
                (etf.exchange_timestamp - book.exchange_timestamp).total_seconds()
            ) * 1_000.0
            max_age = max(max_age, age)
            max_skew = max(max_skew, skew)
            if age > self.config.max_quote_age_ms:
                stale += weight

            if not book.asks:
                creation_missing += weight
                any_one_sided = True
            if not book.bids:
                redemption_missing += weight
                any_one_sided = True

            if book.instrument_state in {
                InstrumentState.SUSPENDED_CONFIRMED,
                InstrumentState.SUSPENDED_SUSPECTED,
            } or book.trading_status in {
                TradingStatus.SUSPENDED,
                TradingStatus.HALTED,
            }:
                suspended += weight
            if (
                book.instrument_state == InstrumentState.LIMIT_UP_LOCKED
                or book.trading_status == TradingStatus.LIMIT_UP
            ) and not book.asks:
                limit_up += weight
            if (
                book.instrument_state == InstrumentState.LIMIT_DOWN_LOCKED
                or book.trading_status == TradingStatus.LIMIT_DOWN
            ) and not book.bids:
                limit_down += weight
            if (
                book.instrument_state == InstrumentState.NORMAL
                and book.raw_status
                and book.state_confidence.value in {"LOW", "MEDIUM"}
            ):
                warnings.append("UNCONFIRMED_VENDOR_STATUS")
            self._check_book(book, critical, component.stock_code)

        if any_one_sided:
            warnings.append("ONE_SIDED_COMPONENT_BOOK")
        if creation_missing > self.config.maximum_missing_weight:
            creation.append("CREATION_MISSING_COMPONENT_ASK")
        if redemption_missing > self.config.maximum_missing_weight:
            redemption.append("REDEMPTION_MISSING_COMPONENT_BID")
        if stale > self.config.maximum_stale_weight:
            warnings.append("COMPONENT_QUOTE_INACTIVITY")
            creation.append("CREATION_STALE_COMPONENT_QUOTES")
            redemption.append("REDEMPTION_STALE_COMPONENT_QUOTES")
        if suspended > self.config.maximum_suspended_weight:
            creation.append("CREATION_SUSPENDED_COMPONENT_WEIGHT")
            redemption.append("REDEMPTION_SUSPENDED_COMPONENT_WEIGHT")
        if limit_up > self.config.maximum_limit_up_weight:
            creation.append("LIMIT_UP_NO_ASK_WEIGHT")
        if limit_down > self.config.maximum_limit_down_weight:
            redemption.append("LIMIT_DOWN_NO_BID_WEIGHT")

        # DATE_HASH is one atomic HMGET. Per-security ctime differences measure
        # inactivity, not receive-time inconsistency of the assembled snapshot.
        if (
            snapshot.source_mode not in {"REDIS_DATE_HASH", "EXECUTABLE_REPLAY"}
            and max_skew > self.config.max_cross_section_skew_ms
        ):
            warnings.append("CROSS_SECTION_EVENT_TIME_SKEW")

        if any(
            component.instrument_id.exchange == Exchange.HKEX
            for component in active
        ):
            self._check_fx(snapshot, common, warnings, etf)

        effective_internal = (
            internal_iopv if internal_iopv is not None else snapshot.internal_iopv
        )
        iopv_error = 0.0
        if snapshot.official_iopv and effective_internal and snapshot.official_iopv > 0:
            iopv_error = (
                abs(effective_internal - snapshot.official_iopv)
                / snapshot.official_iopv
                * 10_000.0
            )
            if iopv_error > self.config.maximum_iopv_error_bps:
                common.append("IOPV_DIVERGENCE")
        elif snapshot.official_iopv is None:
            warnings.append("OFFICIAL_IOPV_MISSING")

        unique_critical = tuple(dict.fromkeys(critical))
        common_all = unique_critical + tuple(dict.fromkeys(common))
        creation_all = tuple(dict.fromkeys((*common_all, *creation)))
        redemption_all = tuple(dict.fromkeys((*common_all, *redemption)))
        all_blockers = tuple(dict.fromkeys((*creation_all, *redemption_all)))
        unique_warnings = tuple(dict.fromkeys(warnings))
        creation_enabled = not creation_all
        redemption_enabled = not redemption_all
        new_trades_enabled = creation_enabled or redemption_enabled
        kill = bool(unique_critical) and self.config.kill_switch_enabled
        status = (
            DataQualityStatus.INVALID
            if unique_critical
            else DataQualityStatus.GOOD
            if creation_enabled and redemption_enabled
            else DataQualityStatus.DEGRADED
        )
        missing = max(creation_missing, redemption_missing)
        return DataQualityReport(
            status=status,
            kill_switch=kill,
            new_trades_enabled=new_trades_enabled,
            creation_enabled=creation_enabled,
            redemption_enabled=redemption_enabled,
            blockers=all_blockers,
            creation_blockers=creation_all,
            redemption_blockers=redemption_all,
            warnings=unique_warnings,
            market_phase=snapshot.market_phase,
            feed_health=snapshot.feed_health,
            source_watermark_age_ms=snapshot.source_watermark_age_ms,
            etf_quote_age_ms=etf_age,
            max_component_quote_age_ms=max_age,
            max_cross_section_skew_ms=max_skew,
            coverage=max(0.0, 1.0 - missing),
            missing_weight=missing,
            creation_missing_weight=creation_missing,
            redemption_missing_weight=redemption_missing,
            stale_weight=stale,
            suspended_weight=suspended,
            limit_up_no_ask_weight=limit_up,
            limit_down_no_bid_weight=limit_down,
            iopv_error_bps=iopv_error,
        )

    def _check_fx(
        self,
        snapshot: MarketSnapshot,
        blockers: list[str],
        warnings: list[str],
        etf: OrderBook,
    ) -> None:
        fx_quote = snapshot.hkd_cny_quote
        if fx_quote is None:
            blockers.append("MISSING_HKD_CNY_QUOTE")
        elif fx_quote.exchange_timestamp is None:
            blockers.append("MISSING_HKD_CNY_TIMESTAMP")
        else:
            fx_age = self._age_ms(
                snapshot.snapshot_timestamp, fx_quote.exchange_timestamp
            )
            if fx_age > self.config.max_quote_age_ms:
                blockers.append("STALE_HKD_CNY_QUOTE")
            fx_skew = abs(
                (etf.exchange_timestamp - fx_quote.exchange_timestamp).total_seconds()
            ) * 1_000.0
            if fx_skew > self.config.max_cross_section_skew_ms:
                warnings.append("HKD_CNY_EVENT_TIME_SKEW")
        if fx_quote is not None and fx_quote.receive_timestamp is None:
            blockers.append("MISSING_HKD_CNY_RECEIVE_TIMESTAMP")

    @staticmethod
    def _notional_weights(
        components: list[PCFComponent],
        books: Dict[str, OrderBook],
    ) -> tuple[dict[str, float], bool]:
        references: dict[str, float] = {}
        observed_prices: list[float] = []
        for component in components:
            book = books.get(component.stock_code)
            price = DataQualityChecker._reference_price(book)
            if price is not None:
                references[component.stock_code] = price
                observed_prices.append(price)
        fallback = median(observed_prices) if observed_prices else 1.0
        missing_reference = False
        notionals: dict[str, float] = {}
        for component in components:
            price = references.get(component.stock_code)
            if price is None:
                price = fallback
                missing_reference = True
            notionals[component.stock_code] = max(
                0.0, float(component.component_share) * float(price)
            )
        total = sum(notionals.values()) or 1.0
        return (
            {code: notional / total for code, notional in notionals.items()},
            missing_reference,
        )

    @staticmethod
    def _reference_price(book: Optional[OrderBook]) -> Optional[float]:
        if book is None:
            return None
        for value in (book.last_price, book.mid_price, book.previous_close):
            if value is not None and value > 0:
                return float(value)
        return None

    @staticmethod
    def _check_book(book: OrderBook, critical: list[str], label: str) -> None:
        levels: Iterable = tuple(book.bids) + tuple(book.asks)
        if any(level.price <= 0 or level.quantity < 0 for level in levels):
            critical.append("ILLEGAL_BOOK_LEVEL:{}".format(label))
        if (
            book.best_bid is not None
            and book.best_ask is not None
            and book.best_bid > book.best_ask
        ):
            critical.append("CROSSED_BOOK:{}".format(label))

    @staticmethod
    def _age_ms(now: datetime, then: datetime) -> float:
        return max(0.0, (now - then).total_seconds() * 1_000.0)

    @staticmethod
    def _china_date(value: datetime):
        if value.tzinfo is None:
            return value.date()
        return value.astimezone(CHINA_TZ).date()
