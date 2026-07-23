"""Marketable IOC depth sweep with explicit insufficient-depth results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

from etf_arbitrage.market_data.models import OrderBookLevel


class SweepSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class DepthSweepResult:
    requested_quantity: float
    filled_quantity: float
    unfilled_quantity: float
    average_fill_price: float
    worst_fill_price: Optional[float]
    total_value: float
    levels_consumed: int
    fully_filled: bool
    failure_reason: Optional[str] = None


def sweep_depth(
    levels: Iterable[OrderBookLevel],
    quantity: float,
    side: SweepSide,
    depth_haircut: float = 1.0,
    slippage_bps: float = 0.0,
) -> DepthSweepResult:
    if quantity < 0:
        raise ValueError("quantity cannot be negative")
    if not 0 < depth_haircut <= 1:
        raise ValueError("depth_haircut must be in (0, 1]")
    if slippage_bps < 0:
        raise ValueError("slippage_bps cannot be negative")
    if quantity == 0:
        return DepthSweepResult(0.0, 0.0, 0.0, 0.0, None, 0.0, 0, True)

    ordered = list(levels)
    if not ordered:
        return DepthSweepResult(
            quantity, 0.0, quantity, 0.0, None, 0.0, 0, False, "EMPTY_BOOK"
        )
    if any(level.price <= 0 or level.quantity < 0 for level in ordered):
        return DepthSweepResult(
            quantity, 0.0, quantity, 0.0, None, 0.0, 0, False, "INVALID_LEVEL"
        )
    ordered.sort(key=lambda item: item.price, reverse=side == SweepSide.SELL)
    remaining = float(quantity)
    filled = 0.0
    total = 0.0
    worst: Optional[float] = None
    consumed = 0
    adverse = 1.0 + slippage_bps / 10_000.0 if side == SweepSide.BUY else 1.0 - slippage_bps / 10_000.0
    for level in ordered:
        available = max(0.0, level.quantity * depth_haircut)
        if available <= 0:
            continue
        take = min(remaining, available)
        fill_price = level.price * adverse
        total += fill_price * take
        filled += take
        remaining -= take
        worst = fill_price
        consumed += 1
        if remaining <= 1e-9:
            remaining = 0.0
            break
    fully_filled = remaining <= 1e-9
    return DepthSweepResult(
        requested_quantity=float(quantity),
        filled_quantity=filled,
        unfilled_quantity=remaining,
        average_fill_price=total / filled if filled else 0.0,
        worst_fill_price=worst,
        total_value=total,
        levels_consumed=consumed,
        fully_filled=fully_filled,
        failure_reason=None if fully_filled else "INSUFFICIENT_DEPTH",
    )
