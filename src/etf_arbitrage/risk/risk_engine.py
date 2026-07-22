"""Risk filters for suspension, price limits and liquidity."""

from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Sequence, Tuple

from etf_arbitrage.config import RiskConfig
from etf_arbitrage.data.models import (
    ComponentWeight,
    ETFQuote,
    LimitStatus,
    StockQuote,
)


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class RiskSnapshot:
    suspension_ratio: float
    missing_quote_ratio: float
    limit_up_ratio: float
    limit_down_ratio: float
    low_liquidity_ratio: float
    etf_spread_bps: float
    etf_amount: float
    score: float
    level: RiskLevel
    blocked: bool
    blockers: Tuple[str, ...]


class RiskEngine:
    def __init__(self, config: RiskConfig = RiskConfig()) -> None:
        self.config = config

    def evaluate(
        self,
        etf_quote: ETFQuote,
        weights: Sequence[ComponentWeight],
        stock_quotes: Mapping[str, StockQuote],
    ) -> RiskSnapshot:
        normalized = self._normalized_map(weights)
        suspension = 0.0
        missing = 0.0
        limit_up = 0.0
        limit_down = 0.0
        low_liquidity = 0.0
        for stock_code, weight in normalized.items():
            quote = stock_quotes.get(stock_code)
            usable_price = bool(
                quote is not None
                and (
                    (quote.last_price is not None and quote.last_price > 0)
                    or (quote.previous_close is not None and quote.previous_close > 0)
                )
            )
            if not usable_price:
                missing += weight
                low_liquidity += weight
                continue
            if quote.is_suspended:
                suspension += weight
            if quote.limit_status == LimitStatus.LIMIT_UP:
                limit_up += weight
            elif quote.limit_status == LimitStatus.LIMIT_DOWN:
                limit_down += weight
            if quote.amount < self.config.min_stock_amount:
                low_liquidity += weight

        blockers = []
        limit_total = limit_up + limit_down
        if suspension + missing > self.config.max_suspension_ratio:
            blockers.append("suspension_or_missing")
        if limit_total > self.config.max_limit_ratio:
            blockers.append("price_limit")
        if low_liquidity > self.config.max_low_liquidity_ratio:
            blockers.append("component_liquidity")
        if not etf_quote.has_executable_quote:
            blockers.append("missing_bid_ask")
        elif etf_quote.spread_bps > self.config.max_etf_spread_bps:
            blockers.append("etf_spread")
        if etf_quote.amount < self.config.min_etf_amount:
            blockers.append("etf_turnover")

        score = min(
            100.0,
            45.0 * min(1.0, (suspension + missing) / max(self.config.max_suspension_ratio, 1e-9))
            + 20.0 * min(1.0, limit_total / max(self.config.max_limit_ratio, 1e-9))
            + 20.0 * min(1.0, low_liquidity / max(self.config.max_low_liquidity_ratio, 1e-9))
            + 15.0
            * (
                1.0
                if not etf_quote.has_executable_quote
                else min(
                    1.0,
                    etf_quote.spread_bps / max(self.config.max_etf_spread_bps, 1e-9),
                )
            ),
        )
        blocked = bool(blockers)
        level = RiskLevel.HIGH if blocked or score >= 65 else RiskLevel.MEDIUM if score >= 30 else RiskLevel.LOW
        return RiskSnapshot(
            suspension_ratio=suspension,
            missing_quote_ratio=missing,
            limit_up_ratio=limit_up,
            limit_down_ratio=limit_down,
            low_liquidity_ratio=low_liquidity,
            etf_spread_bps=etf_quote.spread_bps,
            etf_amount=etf_quote.amount,
            score=score,
            level=level,
            blocked=blocked,
            blockers=tuple(blockers),
        )

    @staticmethod
    def _normalized_map(weights: Sequence[ComponentWeight]) -> Mapping[str, float]:
        total = sum(item.weight for item in weights)
        if total <= 0:
            raise ValueError("Component weights must sum to a positive value")
        return {item.stock_code: item.weight / total for item in weights}
