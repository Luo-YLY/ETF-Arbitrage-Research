"""Optional normalized Redis snapshot adapter, disabled by default."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Iterable, Optional

from etf_arbitrage.executable_config import RedisConfig

from .file_replay import DynamicMarketDataLoader
from .models import DataSourceHealth, MarketSnapshot
from .source import MarketDataSource


class RedisMarketDataSource(MarketDataSource):
    """Read normalized JSON snapshots only; this class never sends orders."""

    def __init__(self, config: RedisConfig, etf_code: str) -> None:
        self.config = config
        self.etf_code = etf_code
        self._client = None
        self._connected = False
        self._running = False
        self._latest: Optional[MarketSnapshot] = None
        self._message = "Redis disabled" if not config.enabled else "not connected"
        self._symbols: set[str] = set()

    def connect(self) -> None:
        if not self.config.enabled:
            self._message = "Redis is disabled by configuration"
            return
        try:
            import redis  # type: ignore

            self._client = redis.Redis(
                host=self.config.host,
                port=self.config.port,
                db=self.config.db,
                password=self.config.password,
                socket_timeout=self.config.socket_timeout_seconds,
                decode_responses=False,
                protocol=2,
            )
            self._client.ping()
            self._connected = True
            self._message = "connected"
        except Exception as exc:
            self._client = None
            self._connected = False
            self._message = "{}: {}".format(type(exc).__name__, exc)

    def disconnect(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
        self._client = None
        self._connected = False
        self._running = False

    def start(self) -> None:
        if not self._connected:
            self.connect()
        self._running = self._connected

    def stop(self) -> None:
        self._running = False

    def subscribe(self, symbols: Iterable[str]) -> None:
        self._symbols.update(str(symbol) for symbol in symbols)

    def get_latest_snapshot(self) -> MarketSnapshot:
        return self.step()

    def step(self) -> MarketSnapshot:
        if not self.config.enabled:
            raise RuntimeError("Redis source is disabled")
        if not self._connected or self._client is None:
            self.connect()
        if not self._connected or self._client is None:
            raise ConnectionError(self._message)
        key = "{}:snapshot:{}".format(self.config.key_prefix, self.etf_code)
        try:
            raw = self._client.get(key)
            if not raw:
                raise KeyError("Redis snapshot key is missing: {}".format(key))
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            frame_rows = payload.get("rows") if isinstance(payload, dict) else payload
            if not isinstance(frame_rows, list):
                raise ValueError("Normalized Redis payload must contain a rows list")
            import pandas as pd

            snapshots, _ = DynamicMarketDataLoader().from_frame(pd.DataFrame(frame_rows))
            if not snapshots:
                raise ValueError("Normalized Redis payload contains no complete snapshot")
            self._latest = snapshots[-1]
            self._message = "connected"
            return self._latest
        except Exception as exc:
            self._message = "{}: {}".format(type(exc).__name__, exc)
            raise

    def health(self) -> DataSourceHealth:
        return DataSourceHealth(
            connected=self._connected,
            running=self._running,
            status="DISABLED" if not self.config.enabled else "RUNNING" if self._running else "READY" if self._connected else "ERROR",
            last_snapshot_time=self._latest.snapshot_timestamp if self._latest else None,
            message=self._message,
        )
