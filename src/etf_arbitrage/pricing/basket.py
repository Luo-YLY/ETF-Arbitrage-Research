"""Direction-specific executable pricing of a PCF basket."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, Mapping, Optional, Tuple

from etf_arbitrage.data.pcf import PCFDocument, SubstituteFlag
from etf_arbitrage.domain import Exchange
from etf_arbitrage.market_data.models import FXQuote, OrderBook

from .depth_sweep import DepthSweepResult, SweepSide, sweep_depth


@dataclass(frozen=True)
class BasketExecutionResult:
    direction: str
    cu_count: int
    physical_value: float
    substitution_cash: float
    estimate_cash_component: float
    total_value: float
    fully_filled: bool
    component_sweeps: Dict[str, DepthSweepResult]
    cash_substituted_symbols: Tuple[str, ...]
    bottleneck_symbol: Optional[str]
    failure_reason: Optional[str]


class ExecutableBasketPricer:
    def __init__(self, pcf: PCFDocument, optional_cash_substitution: bool = False) -> None:
        self.pcf = pcf
        self.optional_cash_substitution = optional_cash_substitution

    def creation_cost(
        self,
        books: Mapping[str, OrderBook],
        cu_count: int = 1,
        depth_haircut: float = 1.0,
        slippage_bps: float = 0.0,
        fx_quote: Optional[FXQuote] = None,
    ) -> BasketExecutionResult:
        return self._price(
            "CREATION", books, cu_count, depth_haircut, slippage_bps, fx_quote
        )

    def redemption_proceeds(
        self,
        books: Mapping[str, OrderBook],
        cu_count: int = 1,
        depth_haircut: float = 1.0,
        slippage_bps: float = 0.0,
        fx_quote: Optional[FXQuote] = None,
    ) -> BasketExecutionResult:
        return self._price(
            "REDEMPTION", books, cu_count, depth_haircut, slippage_bps, fx_quote
        )

    def internal_iopv(
        self,
        books: Mapping[str, OrderBook],
        fx_quote: Optional[FXQuote] = None,
    ) -> float:
        physical = 0.0
        cash = self.pcf.estimate_cash_component
        for component in self.pcf.components:
            if component.substitute_flag.requires_cash_substitution:
                cash += component.creation_cash_substitute
                continue
            if component.component_share <= 0:
                continue
            book = books.get(component.stock_code)
            if book is None or book.mid_price is None or book.mid_price <= 0:
                return float("nan")
            rate = self._fx_rate(component, fx_quote, "MID")
            if rate is None:
                return float("nan")
            physical += component.component_share * book.mid_price * rate
        return (physical + cash) / float(self.pcf.creation_redemption_unit)

    def _price(
        self,
        direction: str,
        books: Mapping[str, OrderBook],
        cu_count: int,
        depth_haircut: float,
        slippage_bps: float,
        fx_quote: Optional[FXQuote],
    ) -> BasketExecutionResult:
        if cu_count <= 0:
            raise ValueError("cu_count must be positive")
        physical_value = 0.0
        substitution_cash = 0.0
        sweeps: Dict[str, DepthSweepResult] = {}
        cash_symbols = []
        failure_reason: Optional[str] = None
        bottleneck: Optional[str] = None
        side = SweepSide.BUY if direction == "CREATION" else SweepSide.SELL
        for component in self.pcf.components:
            book = books.get(component.stock_code)
            use_cash = component.substitute_flag.requires_cash_substitution or (
                self.optional_cash_substitution
                and component.substitute_flag == SubstituteFlag.ALLOWED
            )
            if use_cash:
                fixed_cash = (
                    component.creation_cash_substitute
                    if direction == "CREATION"
                    else component.redemption_cash_substitute
                )
                fx_rate = (
                    1.0
                    if fixed_cash != 0
                    else self._fx_rate(component, fx_quote, direction)
                )
                if fx_rate is None:
                    failure_reason = "MISSING_HKD_CNY_QUOTE"
                    bottleneck = component.stock_code
                    break
                amount = self._cash_amount(component, book, direction, fx_rate)
                substitution_cash += amount * cu_count
                cash_symbols.append(component.stock_code)
                continue
            fx_rate = self._fx_rate(component, fx_quote, direction)
            if fx_rate is None:
                failure_reason = "MISSING_HKD_CNY_QUOTE"
                bottleneck = component.stock_code
                break
            quantity = component.component_share * cu_count
            if quantity <= 0:
                continue
            if book is None:
                failure_reason = "MISSING_COMPONENT_BOOK"
                bottleneck = component.stock_code
                break
            levels = book.asks if side == SweepSide.BUY else book.bids
            result = sweep_depth(levels, quantity, side, depth_haircut, slippage_bps)
            if fx_rate != 1.0:
                result = replace(
                    result,
                    average_fill_price=result.average_fill_price * fx_rate,
                    worst_fill_price=(
                        result.worst_fill_price * fx_rate
                        if result.worst_fill_price is not None
                        else None
                    ),
                    total_value=result.total_value * fx_rate,
                )
            sweeps[component.stock_code] = result
            physical_value += result.total_value
            if not result.fully_filled:
                failure_reason = result.failure_reason
                bottleneck = component.stock_code
                break
        estimate_cash = self.pcf.estimate_cash_component * cu_count
        fully_filled = failure_reason is None
        return BasketExecutionResult(
            direction=direction,
            cu_count=cu_count,
            physical_value=physical_value,
            substitution_cash=substitution_cash,
            estimate_cash_component=estimate_cash,
            total_value=physical_value + substitution_cash + estimate_cash,
            fully_filled=fully_filled,
            component_sweeps=sweeps,
            cash_substituted_symbols=tuple(cash_symbols),
            bottleneck_symbol=bottleneck,
            failure_reason=failure_reason,
        )

    @staticmethod
    def _cash_amount(
        component,
        book: Optional[OrderBook],
        direction: str,
        fx_rate: float,
    ) -> float:
        fixed = (
            component.creation_cash_substitute
            if direction == "CREATION"
            else component.redemption_cash_substitute
        )
        if fixed != 0:
            return fixed
        if component.component_share <= 0 or book is None or book.mid_price is None:
            return 0.0
        market_value = component.component_share * book.mid_price * fx_rate
        if direction == "CREATION":
            return market_value * (1.0 + component.premium_ratio)
        return market_value * (1.0 - component.discount_ratio)

    @staticmethod
    def _fx_rate(component, fx_quote: Optional[FXQuote], direction: str):
        if component.instrument_id.exchange != Exchange.HKEX:
            return 1.0
        if fx_quote is None:
            return None
        if direction == "CREATION":
            return fx_quote.ask
        if direction == "REDEMPTION":
            return fx_quote.bid
        return fx_quote.mid
