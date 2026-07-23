"""PCF-consistent deterministic multi-level simulated market data."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta
from math import exp
from typing import Dict, Iterable, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np

from etf_arbitrage.data.pcf import PCFDocument, SubstituteFlag
from etf_arbitrage.executable_config import SimulationConfig, SimulationScenario

from .models import (
    DataQualityStatus,
    DataSourceHealth,
    MarketSnapshot,
    OrderBook,
    OrderBookLevel,
    TradingStatus,
)
from .source import MarketDataSource


CHINA_TZ = ZoneInfo("Asia/Shanghai")


class SimulatedMarketDataSource(MarketDataSource):
    def __init__(self, pcf: PCFDocument, config: SimulationConfig = SimulationConfig()) -> None:
        self.pcf = pcf
        self.config = config
        self._rng = np.random.default_rng(config.random_seed)
        self._connected = False
        self._running = False
        self._subscribed: set[str] = set()
        self._tick = 0
        self._sequence = 0
        self._latest: Optional[MarketSnapshot] = None
        self._base_prices = self._initial_component_prices()
        self._prices = dict(self._base_prices)
        self._start = datetime.combine(pcf.trading_day, time(9, 30), tzinfo=CHINA_TZ)

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False
        self._running = False

    def start(self) -> None:
        if not self._connected:
            self.connect()
        self._running = True

    def stop(self) -> None:
        self._running = False

    def pause(self) -> None:
        self._running = False

    def resume(self) -> None:
        self.start()

    def reset(self) -> None:
        self._rng = np.random.default_rng(self.config.random_seed)
        self._tick = 0
        self._sequence = 0
        self._latest = None
        self._prices = dict(self._base_prices)

    def subscribe(self, symbols: Iterable[str]) -> None:
        self._subscribed.update(str(symbol) for symbol in symbols)

    def get_latest_snapshot(self) -> MarketSnapshot:
        if self._latest is None:
            return self.step()
        return self._latest

    def step(self) -> MarketSnapshot:
        if not self._connected:
            self.connect()
        self._evolve_prices()
        timestamp = self._start + timedelta(milliseconds=self.config.tick_interval_ms * self._tick)
        self._sequence += self._sequence_increment()
        snapshot = self._build_snapshot(timestamp)
        self._latest = snapshot
        self._tick += 1
        return snapshot

    def snapshot_at_or_after(self, timestamp: datetime) -> Optional[MarketSnapshot]:
        current = self.get_latest_snapshot()
        while current.snapshot_timestamp < timestamp:
            current = self.step()
        return current

    def health(self) -> DataSourceHealth:
        return DataSourceHealth(
            connected=self._connected,
            running=self._running,
            status="RUNNING" if self._running else "READY" if self._connected else "DISCONNECTED",
            last_snapshot_time=self._latest.snapshot_timestamp if self._latest else None,
            message="deterministic simulated feed",
        )

    def _initial_component_prices(self) -> Dict[str, float]:
        active = [
            item
            for item in self.pcf.components
            if item.component_share > 0 and item.substitute_flag != SubstituteFlag.MANDATORY
        ]
        raw = {item.stock_code: float(self._rng.uniform(8.0, 80.0)) for item in active}
        raw_value = sum(item.component_share * raw[item.stock_code] for item in active)
        fixed_cash = sum(
            item.creation_cash_substitute
            for item in self.pcf.components
            if item.substitute_flag == SubstituteFlag.MANDATORY
        )
        target = max(
            1.0,
            (self.pcf.nav_per_creation_unit or raw_value)
            - self.pcf.estimate_cash_component
            - fixed_cash,
        )
        scale = target / raw_value if raw_value > 0 else 1.0
        return {code: max(0.01, price * scale) for code, price in raw.items()}

    def _evolve_prices(self) -> None:
        for code, price in list(self._prices.items()):
            change = float(self._rng.normal(0.0, self.config.base_volatility))
            self._prices[code] = max(0.01, price * (1.0 + change))

    def _premium_bps(self) -> float:
        scenario = self.config.scenario
        magnitude = self.config.premium_shock_bps
        if scenario in {
            SimulationScenario.PREMIUM_SHOCK,
            SimulationScenario.DISCOUNT_SHOCK,
            SimulationScenario.MEAN_REVERSION,
        }:
            if self._tick < self.config.shock_start_tick:
                return 0.0
            elapsed = self._tick - self.config.shock_start_tick
            if elapsed >= self.config.shock_duration_ticks:
                return 0.0
        if scenario == SimulationScenario.PREMIUM_SHOCK:
            return magnitude
        if scenario == SimulationScenario.DISCOUNT_SHOCK:
            return -magnitude
        if scenario == SimulationScenario.MEAN_REVERSION:
            return magnitude * exp(-self.config.mean_reversion_speed * elapsed)
        return float(self._rng.normal(0.0, 0.5))

    def _build_snapshot(self, timestamp: datetime) -> MarketSnapshot:
        internal_iopv = self._internal_iopv()
        etf_mid = internal_iopv * (1.0 + self._premium_bps() / 10_000.0)
        etf_depth = self.config.depth_per_level * self.pcf.creation_redemption_unit
        if self.config.scenario == SimulationScenario.ETF_DEPTH_SHORTAGE:
            etf_depth = self.pcf.creation_redemption_unit * 0.05
        etf_book = self._book(
            self.pcf.etf_code,
            etf_mid,
            etf_depth,
            self.config.etf_spread_bps,
            timestamp,
            TradingStatus.NORMAL,
        )
        component_books: Dict[str, OrderBook] = {}
        active = [
            item
            for item in self.pcf.components
            if item.component_share > 0 and item.substitute_flag != SubstituteFlag.MANDATORY
        ]
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
            if self.config.scenario == SimulationScenario.MISSING_QUOTE and index < missing_count:
                continue
            status = TradingStatus.NORMAL
            if self.config.scenario == SimulationScenario.SUSPENSION and index < special_count:
                status = TradingStatus.SUSPENDED
            if self.config.scenario == SimulationScenario.LIMIT_UP_NO_ASK and index < special_count:
                status = TradingStatus.LIMIT_UP
            if self.config.scenario == SimulationScenario.LIMIT_DOWN_NO_BID and index < special_count:
                status = TradingStatus.LIMIT_DOWN
            book_time = timestamp - timedelta(milliseconds=self.config.quote_latency_ms)
            if self.config.scenario == SimulationScenario.STALE_QUOTE and index < stale_count:
                book_time = timestamp - timedelta(milliseconds=20_000)
            depth = self.config.depth_per_level * max(component.component_share, 100.0)
            if self.config.scenario == SimulationScenario.COMPONENT_DEPTH_SHORTAGE and index == 0:
                depth = max(1.0, component.component_share * 0.05)
            book = self._book(
                component.stock_code,
                self._prices[component.stock_code],
                depth,
                self.config.component_spread_bps,
                book_time,
                status,
            )
            if status == TradingStatus.LIMIT_UP:
                book = replace(book, asks=())
            elif status == TradingStatus.LIMIT_DOWN:
                book = replace(book, bids=())
            component_books[component.stock_code] = book

        crossed = self.config.scenario == SimulationScenario.CROSSED_BOOK
        if crossed and etf_book.bids and etf_book.asks:
            etf_book = replace(
                etf_book,
                bids=(OrderBookLevel(etf_book.asks[0].price + 0.001, etf_book.bids[0].quantity),)
                + etf_book.bids[1:],
            )
        snapshot_time = timestamp
        if self.config.scenario == SimulationScenario.PCF_INVALID:
            snapshot_time = timestamp + timedelta(days=1)
        return MarketSnapshot(
            snapshot_timestamp=snapshot_time,
            etf_order_book=etf_book,
            component_order_books=component_books,
            official_iopv=internal_iopv * (1.0 + float(self._rng.normal(0.0, 0.00002))),
            internal_iopv=internal_iopv,
            data_quality_status=(
                DataQualityStatus.INVALID
                if self.config.scenario == SimulationScenario.DECODE_ERROR
                else DataQualityStatus.GOOD
            ),
            source_mode="SIMULATED",
            sequence_gap=self.config.scenario == SimulationScenario.SEQUENCE_GAP,
            decode_error=(
                "simulated payload decode failure"
                if self.config.scenario == SimulationScenario.DECODE_ERROR
                else None
            ),
        )

    def _book(
        self,
        symbol: str,
        mid: float,
        base_depth: float,
        spread_bps: float,
        timestamp: datetime,
        status: TradingStatus,
    ) -> OrderBook:
        half_spread = max(0.0005, mid * spread_bps / 20_000.0)
        bids = []
        asks = []
        for level in range(max(1, self.config.number_of_book_levels)):
            distance = half_spread * (1.0 + level)
            quantity = max(1.0, base_depth * (self.config.depth_decay ** level))
            bids.append(OrderBookLevel(max(0.001, mid - distance), quantity))
            asks.append(OrderBookLevel(mid + distance, quantity))
        return OrderBook(
            symbol=symbol,
            exchange="SZSE",
            exchange_timestamp=timestamp,
            receive_timestamp=timestamp + timedelta(milliseconds=self.config.quote_latency_ms),
            last_price=mid,
            last_quantity=100.0,
            bids=tuple(bids),
            asks=tuple(asks),
            trading_status=status,
            upper_limit_price=mid * 1.20,
            lower_limit_price=mid * 0.80,
            sequence_number=self._sequence,
            source="simulated",
        )

    def _internal_iopv(self) -> float:
        physical = sum(
            item.component_share * self._prices[item.stock_code]
            for item in self.pcf.components
            if item.component_share > 0 and item.substitute_flag != SubstituteFlag.MANDATORY
        )
        mandatory = sum(
            item.creation_cash_substitute
            for item in self.pcf.components
            if item.substitute_flag == SubstituteFlag.MANDATORY
        )
        return (physical + mandatory + self.pcf.estimate_cash_component) / float(
            self.pcf.creation_redemption_unit
        )

    def _sequence_increment(self) -> int:
        if self.config.scenario == SimulationScenario.SEQUENCE_GAP:
            return 2
        if self.config.sequence_gap_probability > 0 and self._rng.random() < self.config.sequence_gap_probability:
            return 2
        return 1
