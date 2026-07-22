"""Theoretical ETF value calculated from a real PCF creation basket."""

from __future__ import annotations

from typing import List, Mapping, Optional, Sequence, Tuple

from etf_arbitrage.data.models import ComponentWeight, ETFInfo, StockQuote
from etf_arbitrage.data.pcf import PCFComponent, PCFDocument, SubstituteFlag

from .iopv import ComponentValuation, IOPVResult, MissingPricePolicy


class PCFIOPVCalculator:
    """Calculate per-share IOPV from PCF quantities and estimated cash."""

    def __init__(
        self,
        pcf: PCFDocument,
        missing_policy: MissingPricePolicy = MissingPricePolicy.STRICT,
    ) -> None:
        if missing_policy == MissingPricePolicy.REWEIGHT:
            raise ValueError("PCF baskets cannot reweight missing component quantities")
        self.pcf = pcf
        self.missing_policy = MissingPricePolicy(missing_policy)

    def calculate(
        self,
        etf_info: ETFInfo,
        weights: Sequence[ComponentWeight],
        stock_quotes: Mapping[str, StockQuote],
        fallback_prices: Optional[Mapping[str, float]] = None,
    ) -> IOPVResult:
        del weights
        if etf_info.etf_code != self.pcf.etf_code:
            raise ValueError("ETF code does not match PCF SecurityID")
        fallback_prices = fallback_prices or {}
        active = [item for item in self.pcf.components if item.component_share > 0]
        total_quantity = sum(item.component_share for item in active)
        if total_quantity <= 0:
            raise ValueError("PCF does not contain a positive component basket")

        resolved: List[Tuple[PCFComponent, Optional[float], str, bool]] = []
        missing_quantity = 0.0
        for component in active:
            quote = stock_quotes.get(component.stock_code)
            suspended = bool(quote and quote.is_suspended)
            price: Optional[float] = None
            source = "missing"
            if quote is not None and quote.last_price is not None and quote.last_price > 0:
                price = float(quote.last_price)
                source = "suspended_last" if suspended else "live"
            elif (
                quote is not None
                and quote.previous_close is not None
                and quote.previous_close > 0
            ):
                price = float(quote.previous_close)
                source = "previous_close"
            elif component.stock_code in fallback_prices:
                fallback = float(fallback_prices[component.stock_code])
                if fallback > 0:
                    price = fallback
                    source = "fallback"
            if price is None:
                missing_quantity += component.component_share
            resolved.append((component, price, source, suspended))

        missing_weight = missing_quantity / total_quantity
        flags = []
        if missing_quantity > 0:
            flags.append("missing_price")
        if any(suspended for _, _, _, suspended in resolved):
            flags.append("suspended_component")
        if any(
            item.substitute_flag == SubstituteFlag.MANDATORY
            for item in self.pcf.components
        ):
            flags.append("mandatory_cash_substitution")

        if missing_quantity > 0:
            return self._invalid_result(resolved, missing_weight, flags)

        basket_market_value = sum(
            component.component_share * price
            for component, price, _, _ in resolved
            if price is not None
        )
        unit = float(self.pcf.creation_redemption_unit)
        equity_per_share = basket_market_value / unit
        cash_per_share = self.pcf.estimate_cash_component / unit
        theoretical_nav = equity_per_share + cash_per_share

        valuations = []
        suspended_value = 0.0
        for component, price, source, suspended in resolved:
            market_value = component.component_share * price if price is not None else 0.0
            weight = market_value / basket_market_value if basket_market_value > 0 else 0.0
            if suspended:
                suspended_value += market_value
            valuations.append(
                ComponentValuation(
                    stock_code=component.stock_code,
                    raw_weight=weight,
                    effective_weight=weight,
                    price=price,
                    contribution=market_value / unit,
                    price_source=source,
                    is_suspended=suspended,
                )
            )
        suspended_weight = (
            suspended_value / basket_market_value if basket_market_value > 0 else 0.0
        )
        quality = "good" if missing_weight == 0 and suspended_weight == 0 else "degraded"
        if theoretical_nav <= 0:
            flags.append("non_positive_iopv")
        return IOPVResult(
            etf_code=self.pcf.etf_code,
            theoretical_nav=theoretical_nav,
            theoretical_price=theoretical_nav,
            equity_value=equity_per_share,
            cash_per_share=cash_per_share,
            weight_sum=1.0,
            covered_weight=max(0.0, 1.0 - missing_weight),
            missing_weight=missing_weight,
            suspended_weight=suspended_weight,
            valid=theoretical_nav > 0,
            quality=quality,
            flags=tuple(flags),
            components=tuple(valuations),
        )

    def risk_weights(
        self,
        valuation: IOPVResult,
        fallback_weights: Sequence[ComponentWeight],
    ) -> List[ComponentWeight]:
        if valuation.valid and sum(item.raw_weight for item in valuation.components) > 0:
            return [
                ComponentWeight(self.pcf.etf_code, item.stock_code, item.raw_weight)
                for item in valuation.components
            ]
        return list(fallback_weights)

    def _invalid_result(
        self,
        resolved: Sequence[Tuple[PCFComponent, Optional[float], str, bool]],
        missing_weight: float,
        flags: Sequence[str],
    ) -> IOPVResult:
        unit = float(self.pcf.creation_redemption_unit)
        components = tuple(
            ComponentValuation(
                stock_code=component.stock_code,
                raw_weight=component.component_share,
                effective_weight=0.0,
                price=price,
                contribution=(component.component_share * price / unit)
                if price is not None
                else 0.0,
                price_source=source,
                is_suspended=suspended,
            )
            for component, price, source, suspended in resolved
        )
        return IOPVResult(
            etf_code=self.pcf.etf_code,
            theoretical_nav=float("nan"),
            theoretical_price=float("nan"),
            equity_value=float("nan"),
            cash_per_share=self.pcf.estimate_cash_component / unit,
            weight_sum=1.0,
            covered_weight=max(0.0, 1.0 - missing_weight),
            missing_weight=missing_weight,
            suspended_weight=0.0,
            valid=False,
            quality="invalid",
            flags=tuple(flags),
            components=components,
        )
