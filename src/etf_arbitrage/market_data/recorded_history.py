"""Stream recorded last-price snapshots into deterministic synthetic order books."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta
from math import exp, isfinite
from pathlib import Path
from typing import Deque, Dict, Iterable, Iterator, Optional, Union

from etf_arbitrage.data.models import (
    LimitStatus,
    MarketSnapshot as RecordedMarketSnapshot,
)
from etf_arbitrage.data.pcf import PCFDocument, SubstituteFlag
from etf_arbitrage.data.recording import JsonlSnapshotStore
from etf_arbitrage.executable_config import SimulationConfig, SimulationScenario

from .models import (
    DataQualityStatus,
    DataSourceHealth,
    FXQuote,
    MarketSnapshot,
    OrderBookLevel,
    TradingStatus,
)
from .simulated import SimulatedMarketDataSource
from .source import MarketDataSource


class RecordedHistoryMarketDataSource(MarketDataSource):
    """Replay recorded prices while synthesizing executable multi-level books."""

    def __init__(
        self,
        path: Union[Path, str],
        pcf: PCFDocument,
        config: SimulationConfig = SimulationConfig(),
    ) -> None:
        self.path = Path(path).resolve()
        self.pcf = pcf
        self.config = config
        self.store = JsonlSnapshotStore(self.path)
        self.total_records = self.store.count_records()
        if self.total_records <= 0:
            raise ValueError("Local history file is empty: {}".format(self.path))

        self._simulator = SimulatedMarketDataSource(
            pcf,
            config,
            price_seed_source="local_history_replay",
        )
        self._connected = False
        self._running = False
        self._subscribed: set[str] = set()
        self._records: Iterator[RecordedMarketSnapshot]
        self._pending: Deque[MarketSnapshot]
        self._latest: Optional[MarketSnapshot]
        self._previous_prices: Dict[str, float]
        self._records_read = 0
        self._converted = 0
        self._emitted = 0
        self._sequence = 0
        self._reset_reader()

    @property
    def current_tick(self) -> int:
        return self._emitted

    @property
    def records_read(self) -> int:
        return self._records_read

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False
        self._running = False
        self._close_reader()

    def start(self) -> None:
        self.connect()
        self._running = True

    def stop(self) -> None:
        self._running = False

    def reset(self) -> None:
        self._close_reader()
        self._simulator.reset()
        self._reset_reader()

    def subscribe(self, symbols: Iterable[str]) -> None:
        self._subscribed.update(str(symbol) for symbol in symbols)

    def prime(self) -> MarketSnapshot:
        """Validate and queue the first usable recorded snapshot."""
        if not self._pending and self._latest is None:
            self._pending.append(self._next_market_snapshot())
        return self._pending[0] if self._pending else self._latest

    def get_latest_snapshot(self) -> MarketSnapshot:
        if self._latest is None:
            return self.step()
        return self._latest

    def step(self) -> MarketSnapshot:
        if not self._connected:
            self.connect()
        try:
            snapshot = (
                self._pending.popleft()
                if self._pending
                else self._next_market_snapshot()
            )
        except StopIteration:
            self._running = False
            raise StopIteration("End of local history replay")
        self._latest = snapshot
        self._emitted += 1
        return snapshot

    def snapshot_at_or_after(self, timestamp: datetime) -> Optional[MarketSnapshot]:
        if self._latest is not None and self._latest.snapshot_timestamp >= timestamp:
            return self._latest
        for snapshot in self._pending:
            if snapshot.snapshot_timestamp >= timestamp:
                return snapshot
        while True:
            try:
                snapshot = self._next_market_snapshot()
            except StopIteration:
                return None
            self._pending.append(snapshot)
            if snapshot.snapshot_timestamp >= timestamp:
                return snapshot

    def health(self) -> DataSourceHealth:
        return DataSourceHealth(
            connected=self._connected,
            running=self._running,
            status=(
                "RUNNING"
                if self._running
                else "READY"
                if self._connected
                else "DISCONNECTED"
            ),
            last_snapshot_time=(
                self._latest.snapshot_timestamp if self._latest else None
            ),
            message=(
                "local historical prices with synthetic books; "
                "emitted={}/{}; file={}"
            ).format(self._emitted, self.total_records, self.path),
        )

    def _reset_reader(self) -> None:
        self._records = iter(self.store.iter_snapshots())
        self._pending = deque()
        self._latest = None
        self._previous_prices = {}
        self._records_read = 0
        self._converted = 0
        self._emitted = 0
        self._sequence = 0
        self._connected = False
        self._running = False

    def _close_reader(self) -> None:
        records = getattr(self, "_records", None)
        close = getattr(records, "close", None)
        if callable(close):
            close()

    def _next_market_snapshot(self) -> MarketSnapshot:
        while True:
            recorded = next(self._records)
            self._records_read += 1
            self._validate_identity(recorded)
            if not _positive(recorded.etf_quote.last_price):
                continue
            snapshot = self._convert(recorded, self._converted)
            self._converted += 1
            return snapshot

    def _validate_identity(self, recorded: RecordedMarketSnapshot) -> None:
        if recorded.etf_quote.etf_code != self.pcf.etf_code:
            raise ValueError(
                "Recorded ETF code {} does not match PCF {}".format(
                    recorded.etf_quote.etf_code,
                    self.pcf.etf_code,
                )
            )
        if recorded.timestamp.date() != self.pcf.trading_day:
            raise ValueError(
                "Recorded trading day {} does not match PCF {}".format(
                    recorded.timestamp.date(),
                    self.pcf.trading_day,
                )
            )

    def _convert(
        self,
        recorded: RecordedMarketSnapshot,
        tick: int,
    ) -> MarketSnapshot:
        active = [
            component
            for component in self.pcf.components
            if component.component_share > 0
            and not component.substitute_flag.requires_cash_substitution
        ]
        missing_prices = []
        for component in active:
            quote = recorded.stock_quotes.get(component.stock_code)
            price = quote.last_price if quote is not None else None
            if not _positive(price):
                price = quote.previous_close if quote is not None else None
            if not _positive(price):
                price = self._previous_prices.get(component.stock_code)
            if not _positive(price):
                missing_prices.append(component.stock_code)
                continue
            self._previous_prices[component.stock_code] = float(price)

        if missing_prices:
            raise ValueError(
                "Local history lacks initial prices for {} PCF components: {}".format(
                    len(missing_prices),
                    ", ".join(missing_prices[:10]),
                )
            )

        self._simulator._prices = dict(self._previous_prices)
        self._sequence += self._simulator._sequence_increment()
        self._simulator._sequence = self._sequence
        self._simulator._tick = tick
        internal_iopv = self._simulator._internal_iopv()
        etf_mid = self._recorded_etf_mid(
            float(recorded.etf_quote.last_price),
            tick,
        )
        etf_depth = self.config.depth_per_level * self.pcf.creation_redemption_unit
        if self.config.scenario == SimulationScenario.ETF_DEPTH_SHORTAGE:
            etf_depth = self.pcf.creation_redemption_unit * 0.05
        etf_book = self._simulator._book(
            self.pcf.etf_code,
            self.pcf.etf_id.exchange.value,
            etf_mid,
            etf_depth,
            self.config.etf_spread_bps,
            recorded.timestamp,
            TradingStatus.NORMAL,
        )
        if (
            recorded.etf_quote.has_executable_quote
            and etf_book.bids
            and etf_book.asks
        ):
            etf_book = replace(
                etf_book,
                bids=(
                    OrderBookLevel(
                        float(recorded.etf_quote.bid_price),
                        etf_book.bids[0].quantity,
                    ),
                )
                + etf_book.bids[1:],
                asks=(
                    OrderBookLevel(
                        float(recorded.etf_quote.ask_price),
                        etf_book.asks[0].quantity,
                    ),
                )
                + etf_book.asks[1:],
            )

        component_books = {}
        missing_count = int(round(len(active) * self.config.missing_quote_ratio))
        stale_count = int(round(len(active) * self.config.stale_quote_ratio))
        special_ratio = {
            SimulationScenario.SUSPENSION: self.config.suspended_weight,
            SimulationScenario.LIMIT_UP_NO_ASK: self.config.limit_up_weight,
            SimulationScenario.LIMIT_DOWN_NO_BID: self.config.limit_down_weight,
        }.get(self.config.scenario, 0.0)
        special_count = int(round(len(active) * special_ratio))
        if active and special_ratio > 0:
            special_count = max(1, special_count)

        for index, component in enumerate(active):
            quote = recorded.stock_quotes.get(component.stock_code)
            if quote is None:
                continue
            if (
                self.config.scenario == SimulationScenario.MISSING_QUOTE
                and index < missing_count
            ):
                continue
            status = _recorded_status(quote.is_suspended, quote.limit_status)
            if self.config.scenario == SimulationScenario.SUSPENSION and index < special_count:
                status = TradingStatus.SUSPENDED
            if (
                self.config.scenario == SimulationScenario.LIMIT_UP_NO_ASK
                and index < special_count
            ):
                status = TradingStatus.LIMIT_UP
            if (
                self.config.scenario == SimulationScenario.LIMIT_DOWN_NO_BID
                and index < special_count
            ):
                status = TradingStatus.LIMIT_DOWN

            book_time = quote.timestamp
            if (
                self.config.scenario == SimulationScenario.STALE_QUOTE
                and index < stale_count
            ):
                book_time = recorded.timestamp - timedelta(seconds=20)
            depth = self.config.depth_per_level * max(
                component.component_share,
                100.0,
            )
            if (
                self.config.scenario
                == SimulationScenario.COMPONENT_DEPTH_SHORTAGE
                and index == 0
            ):
                depth = max(1.0, component.component_share * 0.05)
            book = self._simulator._book(
                component.stock_code,
                component.instrument_id.exchange.value,
                self._previous_prices[component.stock_code],
                depth,
                self.config.component_spread_bps,
                book_time,
                status,
            )
            if status == TradingStatus.SUSPENDED:
                book = replace(book, bids=(), asks=())
            elif status == TradingStatus.LIMIT_UP:
                book = replace(book, asks=())
            elif status == TradingStatus.LIMIT_DOWN:
                book = replace(book, bids=())
            component_books[component.stock_code] = book

        return MarketSnapshot(
            snapshot_timestamp=recorded.timestamp,
            etf_order_book=etf_book,
            component_order_books=component_books,
            fx_quotes=(
                {
                    "HKD/CNY": FXQuote(
                        bid=self._simulator._fx_bid,
                        ask=self._simulator._fx_ask,
                        exchange_timestamp=recorded.timestamp,
                        receive_timestamp=recorded.timestamp
                        + timedelta(milliseconds=self.config.quote_latency_ms),
                        source="simulated:local_history_replay",
                    )
                }
                if self._simulator._has_hk_components
                else {}
            ),
            official_iopv=None,
            internal_iopv=internal_iopv,
            data_quality_status=(
                DataQualityStatus.INVALID
                if self.config.scenario == SimulationScenario.DECODE_ERROR
                else DataQualityStatus.GOOD
            ),
            source_mode="LOCAL_HISTORY_SYNTHETIC_BOOK",
            sequence_gap=(
                self.config.scenario == SimulationScenario.SEQUENCE_GAP
            ),
            decode_error=(
                "simulated payload decode failure"
                if self.config.scenario == SimulationScenario.DECODE_ERROR
                else None
            ),
        )

    def _recorded_etf_mid(self, price: float, tick: int) -> float:
        scenario = self.config.scenario
        elapsed = tick - self.config.shock_start_tick
        if elapsed < 0 or elapsed >= self.config.shock_duration_ticks:
            return price
        magnitude = self.config.premium_shock_bps / 10_000.0
        if scenario == SimulationScenario.PREMIUM_SHOCK:
            return price * (1.0 + magnitude)
        if scenario == SimulationScenario.DISCOUNT_SHOCK:
            return price * (1.0 - magnitude)
        if scenario == SimulationScenario.MEAN_REVERSION:
            return price * (
                1.0
                + magnitude * exp(-self.config.mean_reversion_speed * elapsed)
            )
        return price


def _recorded_status(
    is_suspended: bool,
    limit_status: LimitStatus,
) -> TradingStatus:
    if is_suspended:
        return TradingStatus.SUSPENDED
    if limit_status == LimitStatus.LIMIT_UP:
        return TradingStatus.LIMIT_UP
    if limit_status == LimitStatus.LIMIT_DOWN:
        return TradingStatus.LIMIT_DOWN
    return TradingStatus.NORMAL


def _positive(value) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return isfinite(number) and number > 0
