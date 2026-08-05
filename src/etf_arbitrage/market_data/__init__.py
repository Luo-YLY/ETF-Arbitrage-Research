"""Unified market data interfaces for executable-arbitrage research."""

from .cross_border import (
    CROSS_BORDER_SNAPSHOT_REQUIRED_FIELDS,
    CrossBorderIndicativeMetrics,
    CrossBorderSnapshotInterface,
    CrossBorderSnapshotSourceMode,
    HKDCNYQuote,
    PendingCrossBorderMarketDataSource,
    calculate_cross_border_indicative_metrics,
    cross_border_hk_components,
    is_virtual_subscription_cash,
    virtual_subscription_cash_component,
)
from .file_replay import DynamicMarketDataLoader, FileReplayMarketDataSource, LoadSummary
from .models import (
    DataQualityStatus,
    DataSourceHealth,
    FXQuote,
    MarketSnapshot,
    OrderBook,
    OrderBookLevel,
    TradingStatus,
)
from .recorded_history import RecordedHistoryMarketDataSource
from .redis_source import RedisMarketDataSource
from .quality import DataQualityChecker, DataQualityReport
from .simulated import SimulatedMarketDataSource
from .source import MarketDataSource

__all__ = [
    "CROSS_BORDER_SNAPSHOT_REQUIRED_FIELDS",
    "CrossBorderIndicativeMetrics",
    "CrossBorderSnapshotInterface",
    "CrossBorderSnapshotSourceMode",
    "HKDCNYQuote",
    "PendingCrossBorderMarketDataSource",
    "calculate_cross_border_indicative_metrics",
    "cross_border_hk_components",
    "DataQualityStatus",
    "DataQualityChecker",
    "DataQualityReport",
    "DataSourceHealth",
    "DynamicMarketDataLoader",
    "FileReplayMarketDataSource",
    "FXQuote",
    "LoadSummary",
    "MarketDataSource",
    "MarketSnapshot",
    "OrderBook",
    "OrderBookLevel",
    "is_virtual_subscription_cash",
    "RecordedHistoryMarketDataSource",
    "RedisMarketDataSource",
    "SimulatedMarketDataSource",
    "TradingStatus",
    "virtual_subscription_cash_component",
]
