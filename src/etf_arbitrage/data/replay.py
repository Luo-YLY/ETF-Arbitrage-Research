"""回测行情数据源接口"""

from datetime import datetime
from typing import Dict, Iterable, List, Optional

import pandas as pd

from .feed import DataFeed
from .models import (
    ComponentWeight,
    ETFInfo,
    ETFQuote,
    LimitStatus,
    MarketSnapshot,
    StockQuote,
)


class DataFrameReplayFeed(DataFeed):
    ETF_COLUMNS = {
        "timestamp",
        "ETF_code",
        "last_price",
        "bid_price",
        "ask_price",
        "volume",
        "amount",
    } # ETF行情数据列
    STOCK_COLUMNS = {"timestamp", "stock_code", "last_price", "volume"} # 成分股行情数据列

    def __init__(
        self,
        etf_info: ETFInfo,
        weights: List[ComponentWeight],
        etf_quotes: pd.DataFrame,
        stock_quotes: pd.DataFrame,
    ) -> None:
        """复制DataFrame数据源，确保数据完整性和时间顺序"""
        self._info = etf_info
        self._weights = list(weights)
        self._etf_quotes = etf_quotes.copy()
        self._stock_quotes = stock_quotes.copy()
        self._validate()

        self._etf_quotes["timestamp"] = pd.to_datetime(self._etf_quotes["timestamp"])
        self._stock_quotes["timestamp"] = pd.to_datetime(self._stock_quotes["timestamp"])
        self._etf_quotes.sort_values("timestamp", inplace=True)
        self._stock_quotes.sort_values(["timestamp", "stock_code"], inplace=True)

    def _validate(self) -> None:
        """验证行情数据的完整性"""
        missing_etf = self.ETF_COLUMNS - set(self._etf_quotes.columns)
        missing_stock = self.STOCK_COLUMNS - set(self._stock_quotes.columns)
        if missing_etf:
            raise ValueError("Missing ETF quote columns: {}".format(sorted(missing_etf)))
        if missing_stock:
            raise ValueError("Missing stock quote columns: {}".format(sorted(missing_stock)))
        if not self._weights:
            raise ValueError("At least one component weight is required")

    def get_etf_info(self, etf_code: str) -> ETFInfo:
        """获取ETF静态信息"""
        if etf_code != self._info.etf_code:
            raise KeyError("Unknown ETF: {}".format(etf_code))
        return self._info

    def get_component_weights(self, etf_code: str) -> List[ComponentWeight]:
        """获取ETF成分股权重"""
        self.get_etf_info(etf_code)
        return list(self._weights)

    def snapshots(
        self,
        etf_code: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
    ) -> Iterable[MarketSnapshot]:
        """按时间顺序，获取ETF及其成分股的市场快照"""
        self.get_etf_info(etf_code)
        etf_rows = self._etf_quotes[self._etf_quotes["ETF_code"] == etf_code]
        if start is not None:
            etf_rows = etf_rows[etf_rows["timestamp"] >= pd.Timestamp(start)]
        if end is not None:
            etf_rows = etf_rows[etf_rows["timestamp"] <= pd.Timestamp(end)]

        # 将成分股行情按时间戳分组，便于快速查找对应的股票行情
        stock_groups: Dict[pd.Timestamp, pd.DataFrame] = {
            timestamp: frame for timestamp, frame in self._stock_quotes.groupby("timestamp")
        }
        # 逐个时点遍历ETF行情数据，生成MarketSnapshot对象
        for row in etf_rows.itertuples(index=False):
            timestamp = pd.Timestamp(row.timestamp)
            stock_frame = stock_groups.get(timestamp, pd.DataFrame())
            stock_map: Dict[str, StockQuote] = {}
            for stock in stock_frame.itertuples(index=False):
                raw_limit = getattr(stock, "limit_status", LimitStatus.NORMAL.value)
                stock_map[str(stock.stock_code)] = StockQuote(
                    timestamp=timestamp.to_pydatetime(),
                    stock_code=str(stock.stock_code),
                    last_price=None if pd.isna(stock.last_price) else float(stock.last_price),
                    volume=float(stock.volume),
                    amount=float(getattr(stock, "amount", 0.0)),
                    turnover_rate=getattr(stock, "turnover_rate", None),
                    is_suspended=bool(getattr(stock, "is_suspended", False)),
                    limit_status=LimitStatus(raw_limit),
                )
            yield MarketSnapshot(
                timestamp=timestamp.to_pydatetime(),
                etf_quote=ETFQuote(
                    timestamp=timestamp.to_pydatetime(),
                    etf_code=str(row.ETF_code),
                    last_price=float(row.last_price),
                    bid_price=None if pd.isna(row.bid_price) else float(row.bid_price),
                    ask_price=None if pd.isna(row.ask_price) else float(row.ask_price),
                    volume=float(row.volume),
                    amount=float(row.amount),
                ),
                stock_quotes=stock_map,
            )
