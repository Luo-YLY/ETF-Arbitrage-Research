from datetime import datetime, timedelta

from etf_arbitrage.market_data import (
    FeedHealthStatus,
    InstrumentState,
    MainlandMarketSchedule,
    MarketPhase,
    MarketSnapshot,
    MarketStateClassifier,
    OrderBook,
    OrderBookLevel,
    StateConfidence,
    TradingStatus,
)


def book(
    symbol: str,
    moment: datetime,
    *,
    bid: float | None = 10.0,
    ask: float | None = 10.01,
    last: float | None = 10.0,
    previous_close: float | None = 10.0,
    raw_status: str = "T0",
    exchange: str = "SZSE",
):
    return OrderBook(
        symbol=symbol,
        exchange=exchange,
        exchange_timestamp=moment,
        receive_timestamp=moment,
        last_price=last,
        bids=(OrderBookLevel(bid, 10_000),) if bid is not None else (),
        asks=(OrderBookLevel(ask, 10_000),) if ask is not None else (),
        trading_status=TradingStatus.UNKNOWN,
        raw_status=raw_status,
        previous_close=previous_close,
        source="REDIS_DATE_HASH",
    )


def test_mainland_schedule_separates_auction_break_and_continuous_phases():
    schedule = MainlandMarketSchedule()

    assert schedule.phase(datetime(2026, 8, 20, 9, 20)) == MarketPhase.OPEN_AUCTION
    assert schedule.phase(datetime(2026, 8, 20, 9, 30)) == MarketPhase.CONTINUOUS
    assert schedule.phase(datetime(2026, 8, 20, 12, 0)) == MarketPhase.BREAK
    assert schedule.phase(datetime(2026, 8, 20, 14, 59)) == MarketPhase.CLOSE_AUCTION
    assert schedule.phase(datetime(2026, 8, 20, 15, 1)) == MarketPhase.CLOSED


def test_sample_derived_t1_without_book_is_suspected_not_confirmed():
    moment = datetime(2026, 8, 20, 10, 0)
    classifier = MarketStateClassifier()
    classified = classifier.classify_book(
        book("002155", moment, bid=None, ask=None, last=None, raw_status="T1"),
        moment,
        MarketPhase.CONTINUOUS,
    )

    assert classified.instrument_state == InstrumentState.SUSPENDED_SUSPECTED
    assert classified.state_confidence == StateConfidence.HIGH
    assert classified.trading_status == TradingStatus.UNKNOWN
    assert "SAMPLE_DERIVED_VENDOR_STATUS" in classified.state_reasons


def test_limit_lock_is_inferred_from_previous_close_and_required_side():
    moment = datetime(2026, 8, 20, 10, 0)
    classifier = MarketStateClassifier()
    up = classifier.classify_book(
        book("300122", moment, bid=12.0, ask=None, last=12.0),
        moment,
        MarketPhase.CONTINUOUS,
    )
    down = classifier.classify_book(
        book("600000", moment, bid=None, ask=9.0, last=9.0, exchange="SSE"),
        moment,
        MarketPhase.CONTINUOUS,
    )

    assert up.instrument_state == InstrumentState.LIMIT_UP_LOCKED
    assert up.upper_limit_price == 12.0
    assert up.trading_status == TradingStatus.LIMIT_UP
    assert down.instrument_state == InstrumentState.LIMIT_DOWN_LOCKED
    assert down.lower_limit_price == 9.0
    assert down.trading_status == TradingStatus.LIMIT_DOWN


def test_close_auction_is_inactive_and_does_not_infer_suspension():
    moment = datetime(2026, 8, 20, 14, 59)
    classified = MarketStateClassifier().classify_book(
        book("002155", moment, bid=None, ask=None, last=None, raw_status="C1"),
        moment,
        MarketPhase.CLOSE_AUCTION,
    )

    assert classified.instrument_state == InstrumentState.INACTIVE_PHASE
    assert classified.state_confidence == StateConfidence.CONFIRMED


def test_snapshot_source_health_uses_market_watermark_not_oldest_component():
    moment = datetime(2026, 8, 20, 10, 0)
    snapshot = MarketSnapshot(
        snapshot_timestamp=moment,
        etf_order_book=book("510300", moment - timedelta(seconds=12), exchange="SSE"),
        component_order_books={
            "600000": book("600000", moment - timedelta(seconds=90), exchange="SSE"),
            "600001": book("600001", moment - timedelta(seconds=2), exchange="SSE"),
        },
        source_mode="REDIS_DATE_HASH",
    )

    classified = MarketStateClassifier().classify_snapshot(snapshot)

    assert classified.market_phase == MarketPhase.CONTINUOUS
    assert classified.feed_health == FeedHealthStatus.HEALTHY
    assert classified.source_watermark_age_ms == 2_000
    assert classified.component_order_books["600000"].quote_inactivity_age_ms == 90_000
