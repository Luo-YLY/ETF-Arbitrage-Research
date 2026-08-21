"""Stateful mainland market-state classification for executable snapshots.

The classifier intentionally separates exchange session phase, instrument state,
and source health.  Vendor codes inferred from captured samples remain
``SUSPENDED_SUSPECTED`` until a vendor codebook confirms their meaning.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo

from .models import (
    FeedHealthStatus,
    InstrumentState,
    MarketPhase,
    MarketSnapshot,
    OrderBook,
    StateConfidence,
    TradingStatus,
)


CHINA_TZ = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class MarketStateConfig:
    suspected_suspension_ms: int = 60_000
    source_watermark_max_age_ms: int = 15_000
    tick_size: float = 0.01


class MainlandMarketSchedule:
    """Conservative session phases shared by the current SSE/SZSE MVP.

    Auction phases are observation-only.  Only ``CONTINUOUS`` is executable.
    Exchange-specific holidays still require a point-in-time trading calendar;
    weekends are closed here and weekdays are treated as candidate trade days.
    """

    open_auction_start = time(9, 15)
    continuous_morning_start = time(9, 30)
    continuous_morning_end = time(11, 30)
    continuous_afternoon_start = time(13, 0)
    close_auction_start = time(14, 57)
    close_time = time(15, 0)

    def phase(self, moment: datetime) -> MarketPhase:
        local = _china_time(moment)
        if local.weekday() >= 5:
            return MarketPhase.CLOSED
        current = local.time().replace(tzinfo=None)
        if current < self.open_auction_start:
            return MarketPhase.PRE_OPEN
        if current < self.continuous_morning_start:
            return MarketPhase.OPEN_AUCTION
        if current <= self.continuous_morning_end:
            return MarketPhase.CONTINUOUS
        if current < self.continuous_afternoon_start:
            return MarketPhase.BREAK
        if current < self.close_auction_start:
            return MarketPhase.CONTINUOUS
        if current <= self.close_time:
            return MarketPhase.CLOSE_AUCTION
        return MarketPhase.CLOSED

    def is_open(self, moment: datetime) -> bool:
        return self.phase(moment) == MarketPhase.CONTINUOUS


class MarketStateClassifier:
    """Enrich order books with auditable state evidence.

    The object is stateful only for sustained no-book inference.  All confirmed
    states must originate from an explicit normalized status.  Sample-derived
    vendor codes such as ``T1`` and ``C1`` remain suspected.
    """

    _ACTIVE_VENDOR_CODES = frozenset({"0", "E0", "T0", "T111", "NORMAL", "TRADING"})
    _SUSPECTED_SUSPENDED_CODES = frozenset({"T1", "C1"})

    def __init__(self, config: MarketStateConfig = MarketStateConfig()) -> None:
        self.config = config
        self.schedule = MainlandMarketSchedule()
        self._no_book_since: dict[str, datetime] = {}

    def reset(self) -> None:
        self._no_book_since.clear()

    def classify_snapshot(self, snapshot: MarketSnapshot) -> MarketSnapshot:
        phase = self.schedule.phase(snapshot.snapshot_timestamp)
        books = [snapshot.etf_order_book, *snapshot.component_order_books.values()]
        event_times = [book.exchange_timestamp for book in books]
        watermark = snapshot.event_watermark or (max(event_times) if event_times else None)
        watermark_age = (
            _age_ms(snapshot.snapshot_timestamp, watermark) if watermark is not None else 0.0
        )
        etf = self.classify_book(snapshot.etf_order_book, snapshot.snapshot_timestamp, phase)
        components = {
            symbol: self.classify_book(book, snapshot.snapshot_timestamp, phase)
            for symbol, book in snapshot.component_order_books.items()
        }
        qualified = {
            key: self.classify_book(book, snapshot.snapshot_timestamp, phase)
            for key, book in snapshot.qualified_component_order_books.items()
        }

        health_reasons: list[str] = []
        if snapshot.decode_error:
            health = FeedHealthStatus.DISCONNECTED
            health_reasons.append("DECODE_ERROR")
        elif snapshot.sequence_gap:
            health = FeedHealthStatus.DISCONNECTED
            health_reasons.append("SEQUENCE_GAP")
        elif (
            phase == MarketPhase.CONTINUOUS
            and watermark is not None
            and watermark_age > self.config.source_watermark_max_age_ms
        ):
            health = FeedHealthStatus.SOURCE_STALE
            health_reasons.append("SOURCE_WATERMARK_STALE")
        elif any(
            reason.startswith("MISSING_RAW_COMPONENT_RECORDS")
            for reason in snapshot.assembly_blockers
        ):
            health = FeedHealthStatus.PARTIAL_MISSING
            health_reasons.extend(snapshot.assembly_blockers)
        else:
            health = FeedHealthStatus.HEALTHY

        return replace(
            snapshot,
            etf_order_book=etf,
            component_order_books=components,
            qualified_component_order_books=qualified,
            market_phase=phase,
            feed_health=health,
            feed_health_reasons=tuple(dict.fromkeys(health_reasons)),
            source_watermark_age_ms=watermark_age,
            event_watermark=watermark,
        )

    def classify_book(
        self,
        book: OrderBook,
        snapshot_time: datetime,
        phase: MarketPhase,
    ) -> OrderBook:
        raw_status = str(book.raw_status or "").strip().upper()
        previous_close = book.previous_close
        upper, lower, limit_source = _price_limits(book, self.config.tick_size)
        inactivity = _age_ms(snapshot_time, book.exchange_timestamp)
        reasons: list[str] = []

        if phase != MarketPhase.CONTINUOUS:
            self._no_book_since.pop(book.symbol, None)
            state = InstrumentState.INACTIVE_PHASE
            confidence = StateConfidence.CONFIRMED
            reasons.append("MARKET_PHASE_{}".format(phase.value))
        elif book.trading_status in {TradingStatus.SUSPENDED, TradingStatus.HALTED}:
            state = InstrumentState.SUSPENDED_CONFIRMED
            confidence = StateConfidence.CONFIRMED
            reasons.append("EXPLICIT_TRADING_STATUS")
        elif _is_limit_up_locked(book, upper, self.config.tick_size):
            self._no_book_since.pop(book.symbol, None)
            state = InstrumentState.LIMIT_UP_LOCKED
            confidence = (
                StateConfidence.CONFIRMED
                if limit_source.startswith("VENDOR")
                else StateConfidence.MEDIUM
            )
            reasons.extend(("BID_AT_UPPER_LIMIT", "NO_ASK_LEVELS", limit_source))
        elif _is_limit_down_locked(book, lower, self.config.tick_size):
            self._no_book_since.pop(book.symbol, None)
            state = InstrumentState.LIMIT_DOWN_LOCKED
            confidence = (
                StateConfidence.CONFIRMED
                if limit_source.startswith("VENDOR")
                else StateConfidence.MEDIUM
            )
            reasons.extend(("ASK_AT_LOWER_LIMIT", "NO_BID_LEVELS", limit_source))
        elif not book.bids and not book.asks:
            first_seen = self._no_book_since.setdefault(book.symbol, snapshot_time)
            no_book_ms = _age_ms(snapshot_time, first_seen)
            if raw_status in self._SUSPECTED_SUSPENDED_CODES:
                state = InstrumentState.SUSPENDED_SUSPECTED
                confidence = StateConfidence.HIGH
                reasons.extend(("SAMPLE_DERIVED_VENDOR_STATUS", "NO_BOOK"))
            elif no_book_ms >= self.config.suspected_suspension_ms:
                state = InstrumentState.SUSPENDED_SUSPECTED
                confidence = StateConfidence.MEDIUM
                reasons.extend(("SUSTAINED_NO_BOOK", "NO_BOOK_MS_{}".format(int(no_book_ms))))
            else:
                state = InstrumentState.ONE_SIDED_UNKNOWN
                confidence = StateConfidence.LOW
                reasons.append("NO_BOOK_UNCONFIRMED")
        elif not book.bids or not book.asks:
            self._no_book_since.pop(book.symbol, None)
            state = InstrumentState.ONE_SIDED_UNKNOWN
            confidence = StateConfidence.LOW
            reasons.append("ONE_SIDED_BOOK_NOT_AT_CONFIRMED_LIMIT")
        else:
            self._no_book_since.pop(book.symbol, None)
            state = InstrumentState.NORMAL
            confidence = (
                StateConfidence.HIGH
                if raw_status in self._ACTIVE_VENDOR_CODES
                else StateConfidence.MEDIUM
            )
            reasons.append("TWO_SIDED_BOOK")

        normalized_status = book.trading_status
        if state == InstrumentState.LIMIT_UP_LOCKED:
            normalized_status = TradingStatus.LIMIT_UP
        elif state == InstrumentState.LIMIT_DOWN_LOCKED:
            normalized_status = TradingStatus.LIMIT_DOWN
        elif state == InstrumentState.SUSPENDED_CONFIRMED:
            normalized_status = TradingStatus.SUSPENDED
        elif state == InstrumentState.NORMAL and normalized_status == TradingStatus.UNKNOWN:
            normalized_status = TradingStatus.NORMAL

        return replace(
            book,
            trading_status=normalized_status,
            previous_close=previous_close,
            upper_limit_price=upper,
            lower_limit_price=lower,
            market_phase=phase,
            instrument_state=state,
            state_confidence=confidence,
            state_reasons=tuple(dict.fromkeys(reason for reason in reasons if reason)),
            quote_inactivity_age_ms=inactivity,
            price_limit_source=limit_source,
        )


def optional_number(record: Mapping[str, Any], *names: str) -> Optional[float]:
    for name in names:
        value = record.get(name)
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            return number
    return None


def _price_limits(book: OrderBook, tick_size: float) -> tuple[Optional[float], Optional[float], str]:
    if book.upper_limit_price and book.lower_limit_price:
        return book.upper_limit_price, book.lower_limit_price, "VENDOR_LIMIT_FIELDS"
    previous = book.previous_close
    if previous is None or previous <= 0:
        return book.upper_limit_price, book.lower_limit_price, ""
    code = book.symbol.split(".", 1)[0].zfill(6)
    ratio: Optional[float]
    if code.startswith(("300", "301", "688", "689")):
        ratio = 0.20
    elif code.startswith(("000", "001", "002", "003", "600", "601", "603", "605")):
        ratio = 0.10
    else:
        ratio = None
    if ratio is None:
        return book.upper_limit_price, book.lower_limit_price, ""
    upper = _round_tick(previous * (1.0 + ratio), tick_size)
    lower = _round_tick(previous * (1.0 - ratio), tick_size)
    return upper, lower, "INFERRED_CODE_PREFIX_{:.0f}PCT".format(ratio * 100.0)


def _round_tick(value: float, tick_size: float) -> float:
    tick = Decimal(str(tick_size))
    units = (Decimal(str(value)) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(units * tick)


def _is_limit_up_locked(book: OrderBook, upper: Optional[float], tick_size: float) -> bool:
    return bool(
        upper is not None
        and book.best_bid is not None
        and book.bids
        and not book.asks
        and abs(book.best_bid - upper) <= tick_size + 1e-12
        and (book.last_price is None or abs(book.last_price - upper) <= tick_size + 1e-12)
    )


def _is_limit_down_locked(book: OrderBook, lower: Optional[float], tick_size: float) -> bool:
    return bool(
        lower is not None
        and book.best_ask is not None
        and book.asks
        and not book.bids
        and abs(book.best_ask - lower) <= tick_size + 1e-12
        and (book.last_price is None or abs(book.last_price - lower) <= tick_size + 1e-12)
    )


def _china_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(CHINA_TZ)


def _age_ms(now: datetime, then: datetime) -> float:
    return max(0.0, (now - then).total_seconds() * 1_000.0)
