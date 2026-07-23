"""One-CU executable ETF creation/redemption arbitrage calculator."""

from __future__ import annotations

from typing import Optional

from etf_arbitrage.data.pcf import PCFDocument
from etf_arbitrage.executable_config import (
    ArbitrageDirection,
    CostConfig,
    DirectionSelection,
    ExecutionConfig,
    ExecutionMode,
)
from etf_arbitrage.market_data import DataQualityChecker, MarketSnapshot
from etf_arbitrage.pricing import ExecutableBasketPricer, SweepSide, sweep_depth

from .models import ArbitrageEvaluation, CapacityResult, DirectionEvaluation


class ExecutableArbitrageDetector:
    def __init__(
        self,
        pcf: PCFDocument,
        execution: ExecutionConfig = ExecutionConfig(),
        costs: CostConfig = CostConfig(),
        quality_checker: Optional[DataQualityChecker] = None,
    ) -> None:
        self.pcf = pcf
        self.execution = execution
        self.costs = costs
        self.quality_checker = quality_checker or DataQualityChecker()
        self.pricer = ExecutableBasketPricer(
            pcf, optional_cash_substitution=execution.optional_cash_substitution
        )

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        cu_count: Optional[int] = None,
        include_capacity: bool = True,
    ) -> ArbitrageEvaluation:
        count = cu_count or self.execution.cu_count
        if count <= 0:
            raise ValueError("cu_count must be positive")
        internal_iopv = self.pricer.internal_iopv(snapshot.component_order_books)
        quality = self.quality_checker.evaluate(snapshot, self.pcf, internal_iopv)
        creation = self._creation(snapshot, count, quality.blockers)
        redemption = self._redemption(snapshot, count, quality.blockers)
        unit_shares = self.pcf.creation_redemption_unit * count
        etf = snapshot.etf_order_book
        last = etf.last_price or etf.mid_price or float("nan")
        bid = etf.best_bid or float("nan")
        ask = etf.best_ask or float("nan")
        premiums = [
            (price - internal_iopv) / internal_iopv
            if internal_iopv > 0 and price == price
            else float("nan")
            for price in (last, bid, ask)
        ]
        creation_capacity = redemption_capacity = None
        if include_capacity:
            creation_capacity = self.capacity(snapshot, ArbitrageDirection.CREATION)
            redemption_capacity = self.capacity(snapshot, ArbitrageDirection.REDEMPTION)
        return ArbitrageEvaluation(
            timestamp=snapshot.snapshot_timestamp,
            etf_code=self.pcf.etf_code,
            internal_iopv=internal_iopv,
            official_iopv=snapshot.official_iopv,
            last_premium=premiums[0],
            bid_premium=premiums[1],
            ask_premium=premiums[2],
            lower_bound=(
                redemption.basket.total_value
                - redemption.estimated_costs
                - redemption.safety_buffer
            )
            / unit_shares,
            upper_bound=(
                creation.basket.total_value
                + creation.estimated_costs
                + creation.safety_buffer
            )
            / unit_shares,
            creation=creation,
            redemption=redemption,
            quality=quality,
            creation_capacity=creation_capacity,
            redemption_capacity=redemption_capacity,
        )

    def capacity(
        self, snapshot: MarketSnapshot, direction: ArbitrageDirection
    ) -> CapacityResult:
        maximum = max(
            1,
            min(self.execution.maximum_cu_per_trade, self.execution.maximum_daily_cu),
        )
        last_profit = 0.0
        marginal = []
        max_executable = 0
        bottleneck_symbol = None
        bottleneck_direction = None
        bottleneck_quantity = 0.0
        for count in range(1, maximum + 1):
            evaluation = self.evaluate(snapshot, count, include_capacity=False)
            result = evaluation.creation if direction == ArbitrageDirection.CREATION else evaluation.redemption
            marginal.append(result.net_profit - last_profit)
            last_profit = result.net_profit
            if result.executable:
                max_executable = count
                continue
            bottleneck_symbol = result.basket.bottleneck_symbol
            bottleneck_direction = "COMPONENT_ASK" if direction == ArbitrageDirection.CREATION else "COMPONENT_BID"
            if not result.etf_sweep.fully_filled:
                bottleneck_symbol = self.pcf.etf_code
                bottleneck_direction = "ETF_BID" if direction == ArbitrageDirection.CREATION else "ETF_ASK"
                bottleneck_quantity = result.etf_sweep.unfilled_quantity
            elif result.basket.bottleneck_symbol:
                sweep = result.basket.component_sweeps.get(result.basket.bottleneck_symbol)
                bottleneck_quantity = sweep.unfilled_quantity if sweep else 0.0
            break
        return CapacityResult(
            direction=direction,
            max_executable_cu=max_executable,
            bottleneck_symbol=bottleneck_symbol,
            bottleneck_direction=bottleneck_direction,
            bottleneck_quantity=bottleneck_quantity,
            marginal_profits=tuple(marginal),
        )

    def _creation(self, snapshot: MarketSnapshot, count: int, quality_blockers) -> DirectionEvaluation:
        basket = self.pricer.creation_cost(
            snapshot.component_order_books,
            count,
            self.execution.depth_haircut,
            self._slippage_bps(),
        )
        quantity = self.pcf.creation_redemption_unit * count
        etf_sweep = sweep_depth(
            snapshot.etf_order_book.bids,
            quantity,
            SweepSide.SELL,
            self.execution.depth_haircut,
            self._slippage_bps(),
        )
        gross = etf_sweep.total_value - basket.total_value
        reference = max(1.0, abs(basket.total_value))
        costs = self._costs(
            basket.physical_value + etf_sweep.total_value,
            reference,
            self.costs.creation_fee_bps,
        )
        safety = reference * self.execution.safety_buffer_bps / 10_000.0
        net = gross - costs - safety
        reasons = list(quality_blockers)
        if self.execution.direction == DirectionSelection.REDEMPTION_ONLY:
            reasons.append("DIRECTION_DISABLED")
        if not self.pcf.creation_allowed:
            reasons.append("CREATION_CLOSED")
        if not self._within_pcf_limit(ArbitrageDirection.CREATION, count):
            reasons.append("CREATION_LIMIT")
        if not basket.fully_filled:
            reasons.append("COMPONENT_ASK_DEPTH")
        if not etf_sweep.fully_filled:
            reasons.append("ETF_BID_DEPTH")
        self._profit_reasons(net, reference, reasons)
        return DirectionEvaluation(
            direction=ArbitrageDirection.CREATION,
            cu_count=count,
            gross_profit=gross,
            estimated_costs=costs,
            safety_buffer=safety,
            net_profit=net,
            net_profit_bps=net / reference * 10_000.0,
            executable=not reasons,
            rejection_reasons=tuple(dict.fromkeys(reasons)),
            basket=basket,
            etf_sweep=etf_sweep,
        )

    def _redemption(self, snapshot: MarketSnapshot, count: int, quality_blockers) -> DirectionEvaluation:
        basket = self.pricer.redemption_proceeds(
            snapshot.component_order_books,
            count,
            self.execution.depth_haircut,
            self._slippage_bps(),
        )
        quantity = self.pcf.creation_redemption_unit * count
        etf_sweep = sweep_depth(
            snapshot.etf_order_book.asks,
            quantity,
            SweepSide.BUY,
            self.execution.depth_haircut,
            self._slippage_bps(),
        )
        gross = basket.total_value - etf_sweep.total_value
        reference = max(1.0, abs(basket.total_value))
        costs = self._costs(
            basket.physical_value + etf_sweep.total_value,
            reference,
            self.costs.redemption_fee_bps,
        )
        safety = reference * self.execution.safety_buffer_bps / 10_000.0
        net = gross - costs - safety
        reasons = list(quality_blockers)
        if self.execution.direction == DirectionSelection.CREATION_ONLY:
            reasons.append("DIRECTION_DISABLED")
        if not self.pcf.redemption_allowed:
            reasons.append("REDEMPTION_CLOSED")
        if not self._within_pcf_limit(ArbitrageDirection.REDEMPTION, count):
            reasons.append("REDEMPTION_LIMIT")
        if not basket.fully_filled:
            reasons.append("COMPONENT_BID_DEPTH")
        if not etf_sweep.fully_filled:
            reasons.append("ETF_ASK_DEPTH")
        self._profit_reasons(net, reference, reasons)
        return DirectionEvaluation(
            direction=ArbitrageDirection.REDEMPTION,
            cu_count=count,
            gross_profit=gross,
            estimated_costs=costs,
            safety_buffer=safety,
            net_profit=net,
            net_profit_bps=net / reference * 10_000.0,
            executable=not reasons,
            rejection_reasons=tuple(dict.fromkeys(reasons)),
            basket=basket,
            etf_sweep=etf_sweep,
        )

    def _costs(self, traded_value: float, reference: float, primary_bps: float) -> float:
        total = traded_value * self.costs.secondary_market_bps / 10_000.0
        total += reference * primary_bps / 10_000.0
        if self.execution.mode == ExecutionMode.INVENTORY_LOCKED:
            total += reference * self.costs.borrowing_bps / 10_000.0
        else:
            total += reference * self.costs.financing_bps / 10_000.0
        return total

    def _profit_reasons(self, net: float, reference: float, reasons: list[str]) -> None:
        if net <= 0:
            reasons.append("NON_POSITIVE_NET_PROFIT")
        if net < self.execution.minimum_profit_amount:
            reasons.append("BELOW_MINIMUM_PROFIT_AMOUNT")
        if net / reference * 10_000.0 < self.execution.minimum_profit_bps:
            reasons.append("BELOW_MINIMUM_PROFIT_BPS")

    def _within_pcf_limit(self, direction: ArbitrageDirection, count: int) -> bool:
        shares = self.pcf.creation_redemption_unit * count
        limits = (
            (self.pcf.creation_limit, self.pcf.net_creation_limit)
            if direction == ArbitrageDirection.CREATION
            else (self.pcf.redemption_limit, self.pcf.net_redemption_limit)
        )
        positive = [value for value in limits if value > 0]
        return not positive or shares <= min(positive)

    def _slippage_bps(self) -> float:
        return {"OPTIMISTIC": 0.0, "BASE": 0.5, "STRESS": 2.0}[
            self.execution.scenario.value
        ]
