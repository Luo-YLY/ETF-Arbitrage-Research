"""数据模型"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, Optional


class LimitStatus(str, Enum):
    """涨跌停状态"""
    NORMAL = "normal"
    LIMIT_UP = "limit_up"
    LIMIT_DOWN = "limit_down"


@dataclass(frozen=True)
class ETFQuote:
    """单个时点ETF二级市场行情数据"""
    timestamp: datetime
    etf_code: str
    last_price: float
    bid_price: float
    ask_price: float
    volume: float
    amount: float

    @property
    def mid_price(self) -> float:
        """优先取买一卖一的中间价，如果买卖一价格无效，则取最新成交价"""
        if self.bid_price > 0 and self.ask_price > 0:
            return (self.bid_price + self.ask_price) / 2.0
        return self.last_price

    @property
    def spread_bps(self) -> float:
        """计算ETF买卖价差（转为基点）"""
        if self.mid_price <= 0:
            return float("inf")
        return (self.ask_price - self.bid_price) / self.mid_price * 10_000.0


@dataclass(frozen=True)
class StockQuote:
    """单个时点单只成分股行情、风险数据"""
    timestamp: datetime
    stock_code: str
    last_price: Optional[float]
    volume: float
    amount: float = 0.0
    turnover_rate: Optional[float] = None
    is_suspended: bool = False
    limit_status: LimitStatus = LimitStatus.NORMAL


@dataclass(frozen=True)
class ETFInfo:
    """计算IOPV、申赎规模所需的ETF静态信息"""
    etf_code: str
    name: str
    exchange: str
    tracking_index: str
    shares: float
    creation_unit: int
    cash_component: float = 0.0


@dataclass(frozen=True)
class ComponentWeight:
    """ETF成分股权重信息"""
    etf_code: str
    stock_code: str
    weight: float


@dataclass(frozen=True)
class MarketSnapshot:
    """同一时点ETF及其成分股的市场行情快照"""
    timestamp: datetime
    etf_quote: ETFQuote
    stock_quotes: Dict[str, StockQuote] = field(default_factory=dict)
