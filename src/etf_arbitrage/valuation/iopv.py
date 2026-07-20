# IOPV 估值

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from etf_arbitrage.data.models import ComponentWeight, ETFInfo, StockQuote


class MissingPricePolicy(str, Enum):
    """缺失价格处理策略"""
    STRICT = "strict" # 严格模式：如果有成分股缺失价格，则IOPV计算无效
    REWEIGHT = "reweight" # 重新加权模式：如果有成分股缺失价格，则按剩余成分股权重重新计算IOPV
    FALLBACK = "fallback" # 回退模式：如果有成分股缺失价格，则使用备用价格计算IOPV，仍然缺失价格的成分股按0处理


@dataclass(frozen=True)
class ComponentValuation:
    """记录单只成分股在本次IOPV计算中的价格来源、有效权重和价值贡献"""
    stock_code: str
    raw_weight: float # 归一化原始权重
    effective_weight: float # 归一化有效权重（可能因缺失价格而重新加权）
    price: Optional[float]
    contribution: float
    price_source: str # 价格来源：live、fallback、suspended_last、missing
    is_suspended: bool # 该成分股是否停牌


@dataclass(frozen=True)
class IOPVResult:
    """封装ETF理论价格、数据质量、风险标志及逐成分估值明细"""
    etf_code: str
    theoretical_nav: float
    theoretical_price: float
    equity_value: float
    cash_per_share: float
    weight_sum: float
    covered_weight: float
    missing_weight: float
    suspended_weight: float
    valid: bool
    quality: str
    flags: Tuple[str, ...]
    components: Tuple[ComponentValuation, ...]


class IOPVCalculator:
    """
    计算ETF理论价格（IOPV）
    有效权重 = 原始权重 × 缺价重分配系数
    股票贡献 = 有效权重 × 股票价格
    股票部分 = Σ股票贡献
    每份现金 = cash_component / shares
    理论净值 = 股票部分 + 每份现金
    理论价格 = 理论净值 × theoretical_price_scale
    """
    def __init__(
        self,
        missing_policy: MissingPricePolicy = MissingPricePolicy.REWEIGHT,
        theoretical_price_scale: float = 1.0,
    ) -> None:
        if theoretical_price_scale <= 0:
            raise ValueError("theoretical_price_scale must be positive")
        self.missing_policy = MissingPricePolicy(missing_policy)
        self.theoretical_price_scale = theoretical_price_scale

    def calculate(
        self,
        etf_info: ETFInfo,
        weights: Sequence[ComponentWeight],
        stock_quotes: Mapping[str, StockQuote],
        fallback_prices: Optional[Mapping[str, float]] = None,
    ) -> IOPVResult:

        # 第一步：检查基金份额，并将指定ETF的成分股权重归一化
        if etf_info.shares <= 0:
            raise ValueError("ETF shares must be positive")
        normalized = self._normalize_weights(etf_info.etf_code, weights)
        fallback_prices = fallback_prices or {}

        # 第二步：逐只成分股寻找实时价格或备用价格，同时累计缺价和停牌权重。
        resolved: List[Tuple[ComponentWeight, Optional[float], str, bool]] = []
        missing_weight = 0.0
        suspended_weight = 0.0
        flags: List[str] = []
        for component in normalized:
            quote = stock_quotes.get(component.stock_code)
            suspended = bool(quote and quote.is_suspended)
            if suspended:
                suspended_weight += component.weight

            price: Optional[float] = None
            source = "missing"
            if quote is not None and quote.last_price is not None and quote.last_price > 0:
                price = float(quote.last_price)
                source = "suspended_last" if suspended else "live"
            elif component.stock_code in fallback_prices:
                fallback = float(fallback_prices[component.stock_code])
                if fallback > 0:
                    price = fallback
                    source = "fallback"

            if price is None:
                missing_weight += component.weight
            resolved.append((component, price, source, suspended))

        if missing_weight > 0:
            flags.append("missing_price")
        if suspended_weight > 0:
            flags.append("suspended_component")

        # 第三步：根据缺价策略判断是终止估值，还是重新分配剩余成分股权重。
        if self.missing_policy == MissingPricePolicy.STRICT and missing_weight > 0:
            return self._invalid_result(
                etf_info, normalized, resolved, missing_weight, suspended_weight, flags
            )
        # 第四步：计算有效权重、成分股价值贡献、总权益价值和理论价格
        covered_weight = 1.0 - missing_weight
        if covered_weight <= 0:
            flags.append("no_usable_component_price")
            return self._invalid_result(
                etf_info, normalized, resolved, missing_weight, suspended_weight, flags
            )

        reweight_factor = (
            1.0 / covered_weight
            if self.missing_policy == MissingPricePolicy.REWEIGHT and missing_weight > 0
            else 1.0
        )
        valuations: List[ComponentValuation] = []
        equity_value = 0.0
        for component, price, source, suspended in resolved:
            effective_weight = component.weight * reweight_factor if price is not None else 0.0
            contribution = effective_weight * price if price is not None else 0.0
            equity_value += contribution
            valuations.append(
                ComponentValuation(
                    stock_code=component.stock_code,
                    raw_weight=component.weight,
                    effective_weight=effective_weight,
                    price=price,
                    contribution=contribution,
                    price_source=source,
                    is_suspended=suspended,
                )
            )
        # 第五步：计算每份基金的现金价值、理论净值和理论价格，并返回IOPVResult对象
        cash_per_share = etf_info.cash_component / etf_info.shares
        theoretical_nav = equity_value + cash_per_share
        if theoretical_nav <= 0:
            flags.append("non_positive_iopv")
        quality = self._quality(missing_weight, suspended_weight)
        return IOPVResult(
            etf_code=etf_info.etf_code,
            theoretical_nav=theoretical_nav,
            theoretical_price=theoretical_nav * self.theoretical_price_scale,
            equity_value=equity_value,
            cash_per_share=cash_per_share,
            weight_sum=sum(item.weight for item in normalized),
            covered_weight=covered_weight,
            missing_weight=missing_weight,
            suspended_weight=suspended_weight,
            valid=theoretical_nav > 0,
            quality=quality,
            flags=tuple(flags),
            components=tuple(valuations),
        )

    @staticmethod
    def _normalize_weights(
        etf_code: str, weights: Sequence[ComponentWeight]
    ) -> List[ComponentWeight]:
        # 保留指定ETF的成分股权重，并归一化为总和为1
        selected = [item for item in weights if item.etf_code == etf_code]
        if not selected:
            raise ValueError("No component weights for ETF {}".format(etf_code))
        if any(item.weight < 0 for item in selected):
            raise ValueError("Component weights cannot be negative")
        total = sum(item.weight for item in selected)
        if total <= 0:
            raise ValueError("Component weights must sum to a positive value")
        return [
            ComponentWeight(item.etf_code, item.stock_code, item.weight / total)
            for item in selected
        ]

    @staticmethod
    def _quality(missing_weight: float, suspended_weight: float) -> str:
        # 根据缺失价格和停牌权重判断数据质量等级
        impaired = max(missing_weight, suspended_weight)
        if impaired <= 0:
            return "good"
        if impaired <= 0.05:
            return "degraded"
        return "poor"

    def _invalid_result(
        self,
        etf_info: ETFInfo,
        weights: Sequence[ComponentWeight],
        resolved: Sequence[Tuple[ComponentWeight, Optional[float], str, bool]],
        missing_weight: float,
        suspended_weight: float,
        flags: Sequence[str],
    ) -> IOPVResult:
        components = tuple(
            ComponentValuation(
                stock_code=item.stock_code,
                raw_weight=item.weight,
                effective_weight=0.0,
                price=price,
                contribution=0.0,
                price_source=source,
                is_suspended=suspended,
            )
            for item, price, source, suspended in resolved
        )
        return IOPVResult(
            etf_code=etf_info.etf_code,
            theoretical_nav=float("nan"),
            theoretical_price=float("nan"),
            equity_value=float("nan"),
            cash_per_share=etf_info.cash_component / etf_info.shares,
            weight_sum=sum(item.weight for item in weights),
            covered_weight=max(0.0, 1.0 - missing_weight),
            missing_weight=missing_weight,
            suspended_weight=suspended_weight,
            valid=False,
            quality="invalid",
            flags=tuple(flags),
            components=components,
        )
