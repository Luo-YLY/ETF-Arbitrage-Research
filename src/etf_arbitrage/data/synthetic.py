# 模拟行情数据测试

from datetime import datetime
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from .models import ComponentWeight, ETFInfo
from .replay import DataFrameReplayFeed


ETF_NAMES: Dict[str, tuple] = {
    "159919": ("沪深300ETF", "沪深300"),
    "159915": ("创业板ETF", "创业板指"),
    "159922": ("中证500ETF", "中证500"),
}


class SyntheticDataFeed(DataFrameReplayFeed):
    """模拟行情数据源，用于回测和研究"""

    def __init__(
        self,
        etf_code: str = "159919",
        periods: int = 180,
        start: Optional[datetime] = None,
        seed: int = 7,
    ) -> None:
        # 生成分钟时间轴，模拟行情数据，包含ETF和成分股的价格、成交量等信息
        if periods < 30:
            raise ValueError("Synthetic feed requires at least 30 periods")
        start = start or datetime(2026, 7, 17, 9, 30)
        timestamps = pd.date_range(start=start, periods=periods, freq="min")
        rng = np.random.default_rng(seed + int(etf_code[-2:]))
        components = ["000001", "000333", "300750"]
        component_weights = np.array([0.40, 0.35, 0.25])

        # 生成成分股价格序列，使用几何布朗运动模型模拟价格波动
        random_steps = rng.normal(0.0, 0.0008, size=(periods, len(components)))
        base_prices = np.array([1.15, 1.34, 1.52])
        prices = base_prices * np.exp(np.cumsum(random_steps, axis=0))
        iopv = prices.dot(component_weights)

        # 生成正溢价、负溢价冲击，模拟ETF价格偏离IOPV的情况
        # 先生成正常的小幅价格噪声，再注入可均值回复的正负偏离，
        # 从而确保演示和集成测试能够识别到溢价、折价及偏离修复。
        premium = rng.normal(0.0, 0.0006, periods)
        premium = self._add_mean_reverting_shock(premium, periods // 3, 0.009, 16)
        premium = self._add_mean_reverting_shock(premium, periods * 2 // 3, -0.007, 13)
        # 计算ETF价格、买一卖一、成交量和成交金额
        etf_price = iopv * (1.0 + premium)
        half_spread = etf_price * 0.00015

        etf_quotes = pd.DataFrame(
            {
                "timestamp": timestamps,
                "ETF_code": etf_code,
                "last_price": etf_price,
                "bid_price": etf_price - half_spread,
                "ask_price": etf_price + half_spread,
                "volume": np.cumsum(rng.integers(50_000, 180_000, periods)),
                "amount": np.cumsum(rng.uniform(5_000_000, 18_000_000, periods)),
            }
        )

        # 令成分股在中间阶段停牌，测试风险阻断
        stock_rows: List[dict] = []
        suspension_start = periods // 2
        for index, timestamp in enumerate(timestamps):
            for component_index, stock_code in enumerate(components):
                suspended = component_index == 2 and suspension_start <= index < suspension_start + 8
                stock_rows.append(
                    {
                        "timestamp": timestamp,
                        "stock_code": stock_code,
                        "last_price": prices[index, component_index],
                        "volume": 0.0 if suspended else float(rng.integers(100_000, 800_000)),
                        "amount": 0.0 if suspended else float(rng.uniform(3_000_000, 30_000_000)),
                        "turnover_rate": 0.0 if suspended else float(rng.uniform(0.001, 0.02)),
                        "is_suspended": suspended,
                        "limit_status": "normal",
                    }
                )
        # 生成ETF静态信息和成分股权重
        name, index_name = ETF_NAMES.get(etf_code, ("深市ETF", "待配置指数"))
        info = ETFInfo(
            etf_code=etf_code,
            name=name,
            exchange="SZSE",
            tracking_index=index_name,
            shares=1_000_000_000.0,
            creation_unit=1_000_000,
        )
        weights = [
            ComponentWeight(etf_code, code, float(weight))
            for code, weight in zip(components, component_weights)
        ]
        super().__init__(info, weights, etf_quotes, pd.DataFrame(stock_rows))

    @staticmethod
    def _add_mean_reverting_shock(
        premium: np.ndarray, start: int, magnitude: float, length: int
    ) -> np.ndarray:
        result = premium.copy()
        end = min(start + length, len(result))
        decay = np.linspace(magnitude, 0.0, end - start, endpoint=False)
        result[start:end] += decay
        return result
