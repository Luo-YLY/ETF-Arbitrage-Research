"""Direction-specific executable pricing of a PCF basket."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Dict, Mapping, Optional, Tuple

from etf_arbitrage.data.pcf import PCFDocument, SubstituteFlag
from etf_arbitrage.domain import Exchange
from etf_arbitrage.market_data.models import (
    FXQuote,
    InstrumentState,
    OrderBook,
    TradingStatus,
)

from .depth_sweep import DepthSweepResult, SweepSide, sweep_depth


@dataclass(frozen=True)
class ComponentExecutionPlan:
    """Direction-specific action chosen for one PCF component."""

    symbol: str
    name: str
    direction: str
    action: "ComponentExecutionAction"
    reason: str
    required_quantity: float
    visible_quantity: float
    filled_quantity: float
    unfilled_quantity: float
    reference_price: Optional[float]
    physical_value: float
    cash_amount: float
    cash_reference_value: float
    substitute_flag: SubstituteFlag
    instrument_state: str
    state_confidence: str


class ComponentExecutionAction(str, Enum):
    PHYSICAL = "PHYSICAL"
    CASH_MANDATORY = "CASH_MANDATORY"
    CASH_ADAPTIVE = "CASH_ADAPTIVE"
    BLOCKED = "BLOCKED"
    NO_ACTION = "NO_ACTION"


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
    component_plans: Dict[str, ComponentExecutionPlan]
    cash_substituted_symbols: Tuple[str, ...]
    adaptive_cash_substituted_symbols: Tuple[str, ...]
    bottleneck_symbol: Optional[str]
    bottleneck_symbols: Tuple[str, ...]
    failure_reason: Optional[str]
    failure_reasons: Tuple[str, ...]
    optional_cash_reference_value: float
    optional_cash_ratio: float
    max_cash_ratio: float


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
            if component.is_virtual_subscription_cash:
                continue
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
        plans: Dict[str, ComponentExecutionPlan] = {}
        cash_symbols: list[str] = []
        adaptive_cash_symbols: list[str] = []
        bottlenecks: list[str] = []
        failures: list[str] = []
        optional_cash_reference_value = 0.0
        side = SweepSide.BUY if direction == "CREATION" else SweepSide.SELL
        for component in self.pcf.components:
            if component.is_virtual_subscription_cash:
                continue
            book = books.get(component.stock_code)
            quantity = component.component_share * cu_count
            reference_price = self._reference_price(book, direction)

            if component.substitute_flag.requires_cash_substitution:
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
                    reason = "MISSING_HKD_CNY_QUOTE"
                    self._add_failure(
                        component.stock_code, reason, bottlenecks, failures
                    )
                    plans[component.stock_code] = self._plan(
                        component,
                        direction,
                        ComponentExecutionAction.BLOCKED,
                        reason,
                        quantity,
                        reference_price,
                        book,
                    )
                    continue
                amount = self._cash_amount(
                    component, book, direction, fx_rate, reference_price
                )
                if amount is None:
                    reason = "MISSING_CASH_SUBSTITUTION_REFERENCE"
                    self._add_failure(
                        component.stock_code, reason, bottlenecks, failures
                    )
                    plans[component.stock_code] = self._plan(
                        component,
                        direction,
                        ComponentExecutionAction.BLOCKED,
                        reason,
                        quantity,
                        reference_price,
                        book,
                    )
                    continue
                total_cash = amount * cu_count
                substitution_cash += total_cash
                cash_symbols.append(component.stock_code)
                plans[component.stock_code] = self._plan(
                    component,
                    direction,
                    ComponentExecutionAction.CASH_MANDATORY,
                    "PCF_MANDATORY_CASH_SUBSTITUTION",
                    quantity,
                    reference_price,
                    book,
                    cash_amount=total_cash,
                )
                continue

            if quantity <= 0:
                plans[component.stock_code] = self._plan(
                    component,
                    direction,
                    ComponentExecutionAction.NO_ACTION,
                    "ZERO_COMPONENT_QUANTITY",
                    quantity,
                    reference_price,
                    book,
                )
                continue

            fx_rate = self._fx_rate(component, fx_quote, direction)
            if fx_rate is None:
                reason = "MISSING_HKD_CNY_QUOTE"
                self._add_failure(
                    component.stock_code, reason, bottlenecks, failures
                )
                plans[component.stock_code] = self._plan(
                    component,
                    direction,
                    ComponentExecutionAction.BLOCKED,
                    reason,
                    quantity,
                    reference_price,
                    book,
                )
                continue
            if book is None:
                reason = "MISSING_COMPONENT_BOOK"
                self._add_failure(
                    component.stock_code, reason, bottlenecks, failures
                )
                plans[component.stock_code] = self._plan(
                    component,
                    direction,
                    ComponentExecutionAction.BLOCKED,
                    reason,
                    quantity,
                    reference_price,
                    book,
                )
                continue
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
            if result.fully_filled:
                physical_value += result.total_value
                plans[component.stock_code] = self._plan(
                    component,
                    direction,
                    ComponentExecutionAction.PHYSICAL,
                    "BOOK_DEPTH_FILLED",
                    quantity,
                    reference_price,
                    book,
                    result=result,
                )
                continue

            reason = self._physical_failure_reason(book, result)
            can_adapt = (
                self.optional_cash_substitution
                and component.substitute_flag == SubstituteFlag.ALLOWED
                and reference_price is not None
                and reference_price > 0
            )
            if can_adapt:
                amount = self._cash_amount(
                    component, book, direction, fx_rate, reference_price
                )
                if amount is not None:
                    total_cash = amount * cu_count
                    reference_value = quantity * reference_price * fx_rate
                    substitution_cash += total_cash
                    optional_cash_reference_value += reference_value
                    cash_symbols.append(component.stock_code)
                    adaptive_cash_symbols.append(component.stock_code)
                    plans[component.stock_code] = self._plan(
                        component,
                        direction,
                        ComponentExecutionAction.CASH_ADAPTIVE,
                        reason,
                        quantity,
                        reference_price,
                        book,
                        result=result,
                        cash_amount=total_cash,
                        cash_reference_value=reference_value,
                    )
                    continue

            self._add_failure(
                component.stock_code, reason, bottlenecks, failures
            )
            physical_value += result.total_value
            plans[component.stock_code] = self._plan(
                component,
                direction,
                ComponentExecutionAction.BLOCKED,
                reason,
                quantity,
                reference_price,
                book,
                result=result,
            )

        optional_cash_ratio = self._optional_cash_ratio(
            optional_cash_reference_value, cu_count, physical_value
        )
        if (
            optional_cash_reference_value > 0
            and optional_cash_ratio > max(0.0, self.pcf.max_cash_ratio) + 1e-12
        ):
            failures.append("MAX_CASH_SUBSTITUTION_RATIO_EXCEEDED")
            bottlenecks.extend(adaptive_cash_symbols)

        failures = list(dict.fromkeys(failures))
        bottlenecks = list(dict.fromkeys(bottlenecks))
        estimate_cash = self.pcf.estimate_cash_component * cu_count
        fully_filled = not failures
        return BasketExecutionResult(
            direction=direction,
            cu_count=cu_count,
            physical_value=physical_value,
            substitution_cash=substitution_cash,
            estimate_cash_component=estimate_cash,
            total_value=physical_value + substitution_cash + estimate_cash,
            fully_filled=fully_filled,
            component_sweeps=sweeps,
            component_plans=plans,
            cash_substituted_symbols=tuple(cash_symbols),
            adaptive_cash_substituted_symbols=tuple(adaptive_cash_symbols),
            bottleneck_symbol=bottlenecks[0] if bottlenecks else None,
            bottleneck_symbols=tuple(bottlenecks),
            failure_reason=failures[0] if failures else None,
            failure_reasons=tuple(failures),
            optional_cash_reference_value=optional_cash_reference_value,
            optional_cash_ratio=optional_cash_ratio,
            max_cash_ratio=max(0.0, self.pcf.max_cash_ratio),
        )

    @staticmethod
    def _cash_amount(
        component,
        book: Optional[OrderBook],
        direction: str,
        fx_rate: float,
        reference_price: Optional[float],
    ) -> Optional[float]:
        fixed = (
            component.creation_cash_substitute
            if direction == "CREATION"
            else component.redemption_cash_substitute
        )
        if fixed != 0:
            return fixed
        if component.component_share <= 0:
            return 0.0
        if book is None or reference_price is None or reference_price <= 0:
            return None
        market_value = component.component_share * reference_price * fx_rate
        if direction == "CREATION":
            return market_value * (1.0 + component.premium_ratio)
        return market_value * (1.0 - component.discount_ratio)

    @staticmethod
    def _reference_price(
        book: Optional[OrderBook], direction: str
    ) -> Optional[float]:
        if book is None:
            return None
        candidates = (
            (book.best_ask, book.last_price, book.best_bid)
            if direction == "CREATION"
            else (book.best_bid, book.last_price, book.best_ask)
        )
        return next(
            (float(value) for value in candidates if value is not None and value > 0),
            None,
        )

    @staticmethod
    def _instrument_state(book: Optional[OrderBook]) -> str:
        if book is None:
            return InstrumentState.UNKNOWN.value
        if book.instrument_state != InstrumentState.UNKNOWN:
            return book.instrument_state.value
        fallback = {
            TradingStatus.SUSPENDED: InstrumentState.SUSPENDED_CONFIRMED,
            TradingStatus.HALTED: InstrumentState.SUSPENDED_CONFIRMED,
            TradingStatus.LIMIT_UP: InstrumentState.LIMIT_UP_LOCKED,
            TradingStatus.LIMIT_DOWN: InstrumentState.LIMIT_DOWN_LOCKED,
        }.get(book.trading_status)
        return fallback.value if fallback is not None else book.instrument_state.value

    @classmethod
    def _physical_failure_reason(
        cls, book: OrderBook, result: DepthSweepResult
    ) -> str:
        state = cls._instrument_state(book)
        if state not in {InstrumentState.NORMAL.value, InstrumentState.UNKNOWN.value}:
            return state
        return result.failure_reason or "INSUFFICIENT_DEPTH"

    @classmethod
    def _plan(
        cls,
        component,
        direction: str,
        action: ComponentExecutionAction,
        reason: str,
        quantity: float,
        reference_price: Optional[float],
        book: Optional[OrderBook],
        result: Optional[DepthSweepResult] = None,
        cash_amount: float = 0.0,
        cash_reference_value: float = 0.0,
    ) -> ComponentExecutionPlan:
        levels = ()
        if book is not None:
            levels = book.asks if direction == "CREATION" else book.bids
        visible_quantity = sum(max(0.0, level.quantity) for level in levels)
        return ComponentExecutionPlan(
            symbol=component.stock_code,
            name=component.symbol,
            direction=direction,
            action=action,
            reason=reason,
            required_quantity=float(quantity),
            visible_quantity=visible_quantity,
            filled_quantity=result.filled_quantity if result is not None else 0.0,
            unfilled_quantity=(
                result.unfilled_quantity
                if result is not None
                else float(quantity)
                if action == ComponentExecutionAction.BLOCKED
                else 0.0
            ),
            reference_price=reference_price,
            physical_value=result.total_value if result is not None else 0.0,
            cash_amount=cash_amount,
            cash_reference_value=cash_reference_value,
            substitute_flag=component.substitute_flag,
            instrument_state=cls._instrument_state(book),
            state_confidence=(
                book.state_confidence.value if book is not None else "UNKNOWN"
            ),
        )

    def _optional_cash_ratio(
        self,
        optional_cash_reference_value: float,
        cu_count: int,
        physical_value: float,
    ) -> float:
        denominator = abs(self.pcf.nav_per_creation_unit) * cu_count
        if denominator <= 0:
            denominator = max(1.0, abs(physical_value) + optional_cash_reference_value)
        return optional_cash_reference_value / denominator

    @staticmethod
    def _add_failure(
        symbol: str,
        reason: str,
        bottlenecks: list[str],
        failures: list[str],
    ) -> None:
        bottlenecks.append(symbol)
        failures.append(reason)

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
