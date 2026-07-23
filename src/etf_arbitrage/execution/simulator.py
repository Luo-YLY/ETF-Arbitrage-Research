"""Convert fully executable depth sweeps into paper orders and fills."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from etf_arbitrage.arbitrage.models import DirectionEvaluation
from etf_arbitrage.data.pcf import PCFDocument
from etf_arbitrage.executable_config import ArbitrageDirection

from .models import SimulatedFill, SimulatedOrder


class PaperExecutionSimulator:
    ORDER_TYPE = "MARKETABLE_IOC"
    FILL_POLICY = "VIRTUAL_ALL_OR_NONE"

    def build(
        self,
        cycle_id: str,
        result: DirectionEvaluation,
        pcf: PCFDocument,
        fill_time: datetime,
    ) -> tuple[tuple[SimulatedOrder, ...], tuple[SimulatedFill, ...]]:
        if not result.basket.fully_filled or not result.etf_sweep.fully_filled:
            raise ValueError("Virtual all-or-none execution requires complete depth")
        orders = []
        fills = []
        component_side = "BUY" if result.direction == ArbitrageDirection.CREATION else "SELL"
        for symbol, sweep in result.basket.component_sweeps.items():
            order = self._order(cycle_id, symbol, component_side, sweep.requested_quantity, fill_time)
            orders.append(order)
            fills.append(self._fill(order, sweep.average_fill_price, sweep.total_value, sweep.levels_consumed, fill_time))
        etf_side = "SELL" if result.direction == ArbitrageDirection.CREATION else "BUY"
        etf_order = self._order(
            cycle_id, pcf.etf_code, etf_side, result.etf_sweep.requested_quantity, fill_time
        )
        orders.append(etf_order)
        fills.append(
            self._fill(
                etf_order,
                result.etf_sweep.average_fill_price,
                result.etf_sweep.total_value,
                result.etf_sweep.levels_consumed,
                fill_time,
            )
        )
        return tuple(orders), tuple(fills)

    @staticmethod
    def _order(cycle_id: str, symbol: str, side: str, quantity: float, submit_time: datetime) -> SimulatedOrder:
        return SimulatedOrder(
            order_id="ord-{}".format(uuid4().hex[:12]),
            cycle_id=cycle_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            order_type=PaperExecutionSimulator.ORDER_TYPE,
            submit_time=submit_time,
        )

    @staticmethod
    def _fill(order: SimulatedOrder, average_price: float, total_value: float, levels: int, fill_time: datetime) -> SimulatedFill:
        return SimulatedFill(
            fill_id="fill-{}".format(uuid4().hex[:12]),
            order_id=order.order_id,
            cycle_id=order.cycle_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            average_price=average_price,
            total_value=total_value,
            fill_time=fill_time,
            levels_consumed=levels,
        )
