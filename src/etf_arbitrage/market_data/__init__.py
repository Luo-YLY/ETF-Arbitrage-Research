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
from .executable_replay import ExecutableRecordingReplayMarketDataSource
from .models import (
    DataQualityStatus,
    DataSourceHealth,
    FeedHealthStatus,
    FXQuote,
    InstrumentState,
    MarketPhase,
    MarketSnapshot,
    OrderBook,
    OrderBookLevel,
    StateConfidence,
    TradingStatus,
)
from .recorded_history import RecordedHistoryMarketDataSource
from .redis_source import NoNewSnapshotError, RedisMarketDataSource
from .quality import DataQualityChecker, DataQualityReport
from .simulated import SimulatedMarketDataSource
from .source import MarketDataSource
from .state import MainlandMarketSchedule, MarketStateClassifier, MarketStateConfig

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
    "FeedHealthStatus",
    "DynamicMarketDataLoader",
    "ExecutableRecordingReplayMarketDataSource",
    "FileReplayMarketDataSource",
    "FXQuote",
    "InstrumentState",
    "LoadSummary",
    "MarketDataSource",
    "MainlandMarketSchedule",
    "MarketPhase",
    "MarketSnapshot",
    "MarketStateClassifier",
    "MarketStateConfig",
    "NoNewSnapshotError",
    "OrderBook",
    "OrderBookLevel",
    "is_virtual_subscription_cash",
    "RecordedHistoryMarketDataSource",
    "RedisMarketDataSource",
    "SimulatedMarketDataSource",
    "StateConfidence",
    "TradingStatus",
    "virtual_subscription_cash_component",
]
