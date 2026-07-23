"""Abstract market-data source contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Iterable, Optional

from .models import DataSourceHealth, MarketSnapshot


class MarketDataSource(ABC):
    @abstractmethod
    def connect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def disconnect(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def start(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_snapshot(self) -> MarketSnapshot:
        raise NotImplementedError

    @abstractmethod
    def step(self) -> MarketSnapshot:
        raise NotImplementedError

    @abstractmethod
    def subscribe(self, symbols: Iterable[str]) -> None:
        raise NotImplementedError

    @abstractmethod
    def health(self) -> DataSourceHealth:
        raise NotImplementedError

    def snapshot_at_or_after(self, timestamp: datetime) -> Optional[MarketSnapshot]:
        del timestamp
        return None
