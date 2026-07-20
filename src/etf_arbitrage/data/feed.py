"""数据源接口，支持回测和实盘行情数据源"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Iterable, List, Optional

from .models import ComponentWeight, ETFInfo, MarketSnapshot


class DataFeed(ABC):
    """数据源接口"""

    @abstractmethod
    def get_etf_info(self, etf_code: str) -> ETFInfo:
        """获取ETF静态信息：基金份额、跟踪指数、最小申赎单位"""
        raise NotImplementedError

    @abstractmethod
    def get_component_weights(self, etf_code: str) -> List[ComponentWeight]:
        """获取ETF成分股权重"""
        raise NotImplementedError

    @abstractmethod
    def snapshots(
        self,
        etf_code: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Iterable[MarketSnapshot]:
        """按时间顺序，获取ETF及其成分股的市场快照"""
        raise NotImplementedError


class LiveDataFeed(DataFeed):
    """实盘行情数据源接口，预留位置"""

    def get_etf_info(self, etf_code: str) -> ETFInfo:
        raise NotImplementedError("Connect a live ETF master-data adapter")

    def get_component_weights(self, etf_code: str) -> List[ComponentWeight]:
        raise NotImplementedError("Connect a live PCF/component adapter")

    def snapshots(
        self,
        etf_code: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Iterable[MarketSnapshot]:
        raise NotImplementedError("Connect a live quote stream adapter")
