"""PCF-consistent deterministic multi-level simulated market data."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta
from math import exp
from typing import Dict, Iterable, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

import numpy as np

from etf_arbitrage.data.pcf import PCFDocument, SubstituteFlag
from etf_arbitrage.domain import Exchange
from etf_arbitrage.executable_config import SimulationConfig, SimulationScenario

from .models import (
    DataQualityStatus,
    DataSourceHealth,
    FXQuote,
    MarketSnapshot,
    OrderBook,
    OrderBookLevel,
    TradingStatus,
)
from .source import MarketDataSource


CHINA_TZ = ZoneInfo("Asia/Shanghai")


class SimulatedMarketDataSource(MarketDataSource):
    def __init__(
        self,
        pcf: PCFDocument,
        config: SimulationConfig = SimulationConfig(),
        initial_component_prices: Optional[Mapping[str, float]] = None,
        initial_etf_price: Optional[float] = None,
        initial_hkd_cny_bid: Optional[float] = None,
        initial_hkd_cny_ask: Optional[float] = None,
        price_seed_source: str = "synthetic",
    ) -> None:
        self.pcf = pcf
        self.config = config
        self._rng = np.random.default_rng(config.random_seed)
        self._connected = False
        self._running = False
        self._subscribed: set[str] = set()
        self._tick = 0
        self._sequence = 0
        self._latest: Optional[MarketSnapshot] = None
        self.price_seed_source = str(price_seed_source)
        self._has_hk_components = any(
            item.instrument_id.exchange == Exchange.HKEX
            and item.component_share > 0
            and not item.substitute_flag.requires_cash_substitution
            for item in self.pcf.components
        )
        self._set_initial_fx(initial_hkd_cny_bid, initial_hkd_cny_ask)
        self._base_fx_mid = self._fx_mid
        self._base_fx_bid = self._fx_bid
        self._base_fx_ask = self._fx_ask
        self._base_prices = self._initial_component_prices(initial_component_prices)
        self._prices = dict(self._base_prices)
        self.initial_etf_price = _optional_positive_price(
            initial_etf_price, "initial ETF price"
        )
        base_iopv = self._internal_iopv()
        self.base_premium_bps = (
            (self.initial_etf_price / base_iopv - 1.0) * 10_000.0
            if self.initial_etf_price is not None
            else 0.0
        )
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
        self._fx_mid = self._base_fx_mid
        self._fx_bid = self._base_fx_bid
        self._fx_ask = self._base_fx_ask

    def subscribe(self, symbols: Iterable[str]) -> None:
        self._subscribed.update(str(symbol) for symbol in symbols)

    def get_latest_snapshot(self) -> MarketSnapshot:
        if self._latest is None:
            return self.step()
        return self._latest

    @property
    def current_tick(self) -> int:
        """Number of snapshots generated on the finite simulation timeline."""
        return self._tick

    def step(self) -> MarketSnapshot:
        if self._tick >= self.config.total_ticks:
            self._running = False
            raise StopIteration("End of simulated timeline")
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
            message="deterministic simulated feed; price seed={}".format(
                self.price_seed_source
            ),
        )

    def _initial_component_prices(
        self,
        initial_component_prices: Optional[Mapping[str, float]],
    ) -> Dict[str, float]:
        active = [
            item
            for item in self.pcf.components
            if item.component_share > 0
            and not item.substitute_flag.requires_cash_substitution
        ]
        if initial_component_prices is not None:
            missing = [
                item.stock_code
                for item in active
                if item.stock_code not in initial_component_prices
            ]
            if missing:
                raise ValueError(
                    "initial component prices are missing: {}".format(
                        ", ".join(missing[:8])
                    )
                )
            return {
                item.stock_code: _positive_price(
                    initial_component_prices[item.stock_code],
                    "initial component price {}".format(item.stock_code),
                )
                for item in active
            }
        raw = {item.stock_code: float(self._rng.uniform(8.0, 80.0)) for item in active}
        raw_value = sum(
            item.component_share
            * raw[item.stock_code]
            * (
                self._fx_mid
                if item.instrument_id.exchange == Exchange.HKEX
                else 1.0
            )
            for item in active
        )
        fixed_cash = sum(
            item.creation_cash_substitute
            for item in self.pcf.components
            if item.substitute_flag.requires_cash_substitution
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
        if self._tick == 0:
            return
        for code, price in list(self._prices.items()):
            change = float(self._rng.normal(0.0, self.config.base_volatility))
            self._prices[code] = max(0.01, price * (1.0 + change))
        if self._has_hk_components and self.config.hkd_cny_volatility > 0:
            fx_change = float(
                self._rng.normal(0.0, self.config.hkd_cny_volatility)
            )
            self._fx_mid = max(0.0001, self._fx_mid * (1.0 + fx_change))
            self._set_fx_spread()

    def _premium_bps(self) -> float:
        scenario = self.config.scenario
        magnitude = self.config.premium_shock_bps
        if scenario in {
            SimulationScenario.PREMIUM_SHOCK,
            SimulationScenario.DISCOUNT_SHOCK,
            SimulationScenario.MEAN_REVERSION,
        }:
            if self._tick < self.config.shock_start_tick:
                return self.base_premium_bps
            elapsed = self._tick - self.config.shock_start_tick
            if elapsed >= self.config.shock_duration_ticks:
                return self.base_premium_bps
        if scenario == SimulationScenario.PREMIUM_SHOCK:
            return self.base_premium_bps + magnitude
        if scenario == SimulationScenario.DISCOUNT_SHOCK:
            return self.base_premium_bps - magnitude
        if scenario == SimulationScenario.MEAN_REVERSION:
            return self.base_premium_bps + magnitude * exp(
                -self.config.mean_reversion_speed * elapsed
            )
        if self.initial_etf_price is not None and self._tick == 0:
            return self.base_premium_bps
        return self.base_premium_bps + float(self._rng.normal(0.0, 0.5))

    def _build_snapshot(self, timestamp: datetime) -> MarketSnapshot:
        internal_iopv = self._internal_iopv()
        etf_mid = internal_iopv * (1.0 + self._premium_bps() / 10_000.0)
        etf_depth = self.config.depth_per_level * self.pcf.creation_redemption_unit
        if self.config.scenario == SimulationScenario.ETF_DEPTH_SHORTAGE:
            etf_depth = self.pcf.creation_redemption_unit * 0.05
        etf_book = self._book(
            self.pcf.etf_code,
            self.pcf.etf_id.exchange.value,
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
            if item.component_share > 0
            and not item.substitute_flag.requires_cash_substitution
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
                component.instrument_id.exchange.value,
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
            fx_quotes=(
                {
                    "HKD/CNY": FXQuote(
                        bid=self._fx_bid,
                        ask=self._fx_ask,
                        exchange_timestamp=timestamp,
                        receive_timestamp=timestamp
                        + timedelta(milliseconds=self.config.quote_latency_ms),
                        source="simulated:{}".format(self.price_seed_source),
                    )
                }
                if self._has_hk_components
                else {}
            ),
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
        exchange: str,
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
            exchange=exchange,
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
            source="simulated:{}".format(self.price_seed_source),
        )

    def _internal_iopv(self) -> float:
        physical = sum(
            item.component_share
            * self._prices[item.stock_code]
            * (
                self._fx_mid
                if item.instrument_id.exchange == Exchange.HKEX
                else 1.0
            )
            for item in self.pcf.components
            if item.component_share > 0
            and not item.substitute_flag.requires_cash_substitution
        )
        mandatory = sum(
            item.creation_cash_substitute
            for item in self.pcf.components
            if item.substitute_flag.requires_cash_substitution
        )
        return (physical + mandatory + self.pcf.estimate_cash_component) / float(
            self.pcf.creation_redemption_unit
        )

    def _set_initial_fx(
        self,
        initial_bid: Optional[float],
        initial_ask: Optional[float],
    ) -> None:
        if not self._has_hk_components:
            self._fx_mid = 1.0
            self._fx_bid = 1.0
            self._fx_ask = 1.0
            return
        if (initial_bid is None) != (initial_ask is None):
            raise ValueError("HKD/CNY bid and ask must be supplied together")
        if initial_bid is not None and initial_ask is not None:
            quote = FXQuote(bid=float(initial_bid), ask=float(initial_ask))
            self._fx_mid = quote.mid
            self._fx_bid = quote.bid
            self._fx_ask = quote.ask
            return
        self._fx_mid = _positive_price(
            self.config.hkd_cny_mid, "simulated HKD/CNY mid"
        )
        self._set_fx_spread()

    def _set_fx_spread(self) -> None:
        spread_bps = float(self.config.hkd_cny_spread_bps)
        if spread_bps < 0:
            raise ValueError("hkd_cny_spread_bps cannot be negative")
        half = self._fx_mid * spread_bps / 20_000.0
        self._fx_bid = self._fx_mid - half
        self._fx_ask = self._fx_mid + half

    def _sequence_increment(self) -> int:
        if self.config.scenario == SimulationScenario.SEQUENCE_GAP:
            return 2
        if self.config.sequence_gap_probability > 0 and self._rng.random() < self.config.sequence_gap_probability:
            return 2
        return 1


def _positive_price(value: float, label: str) -> float:
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("{} must be numeric".format(label)) from exc
    if not np.isfinite(price) or price <= 0:
        raise ValueError("{} must be finite and positive".format(label))
    return price


def _optional_positive_price(value: Optional[float], label: str) -> Optional[float]:
    return None if value is None else _positive_price(value, label)
