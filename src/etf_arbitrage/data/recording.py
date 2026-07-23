"""Append-only storage for live market snapshots."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple, Union

import pandas as pd

from .models import ETFQuote, LimitStatus, MarketSnapshot, StockQuote


class JsonlSnapshotStore:
    """Persist complete market snapshots as deduplicated JSON Lines records."""

    SCHEMA_VERSION = 1

    def __init__(self, path: Union[Path, str]) -> None:
        self.path = Path(path)
        self._last_fingerprint: Optional[str] = None
        self._last_fingerprint_loaded = False

    def append(
        self,
        snapshot: MarketSnapshot,
        captured_at: Optional[datetime] = None,
    ) -> bool:
        payload = self._serialize(snapshot, captured_at or datetime.now())
        fingerprint = self._fingerprint(payload)
        self._load_last_fingerprint()
        if fingerprint == self._last_fingerprint:
            return False

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
        self._last_fingerprint = fingerprint
        return True

    def iter_snapshots(self) -> Iterable[MarketSnapshot]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                    yield self._deserialize(payload)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ValueError(
                        "Invalid snapshot record at line {} in {}".format(
                            line_number, self.path
                        )
                    ) from exc

    def to_frames(self, etf_code: Optional[str] = None) -> Tuple[pd.DataFrame, pd.DataFrame]:
        etf_rows = []
        stock_rows = []
        seen = set()
        for snapshot in self.iter_snapshots():
            quote = snapshot.etf_quote
            if etf_code is not None and quote.etf_code != etf_code:
                continue
            fingerprint = self._snapshot_identity(snapshot)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            snapshot_id = len(etf_rows)
            etf_rows.append(
                {
                    "snapshot_id": snapshot_id,
                    "timestamp": snapshot.timestamp,
                    "ETF_code": quote.etf_code,
                    "last_price": quote.last_price,
                    "bid_price": quote.bid_price,
                    "ask_price": quote.ask_price,
                    "volume": quote.volume,
                    "amount": quote.amount,
                }
            )
            for stock in snapshot.stock_quotes.values():
                stock_rows.append(
                    {
                        "snapshot_id": snapshot_id,
                        "timestamp": snapshot.timestamp,
                        "stock_code": stock.stock_code,
                        "last_price": stock.last_price,
                        "previous_close": stock.previous_close,
                        "volume": stock.volume,
                        "amount": stock.amount,
                        "turnover_rate": stock.turnover_rate,
                        "is_suspended": stock.is_suspended,
                        "limit_status": stock.limit_status.value,
                    }
                )
        return pd.DataFrame(etf_rows), pd.DataFrame(stock_rows)

    def _load_last_fingerprint(self) -> None:
        if self._last_fingerprint_loaded:
            return
        self._last_fingerprint_loaded = True
        if not self.path.exists():
            return
        last_payload: Optional[Dict[str, Any]] = None
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last_payload = json.loads(line)
        if last_payload is not None:
            self._last_fingerprint = self._fingerprint(last_payload)

    @classmethod
    def _serialize(cls, snapshot: MarketSnapshot, captured_at: datetime) -> Dict[str, Any]:
        etf = snapshot.etf_quote
        return {
            "schema_version": cls.SCHEMA_VERSION,
            "captured_at": captured_at.isoformat(),
            "timestamp": snapshot.timestamp.isoformat(),
            "etf_quote": {
                "etf_code": etf.etf_code,
                "last_price": etf.last_price,
                "bid_price": etf.bid_price,
                "ask_price": etf.ask_price,
                "volume": etf.volume,
                "amount": etf.amount,
            },
            "stock_quotes": [
                {
                    "stock_code": stock.stock_code,
                    "timestamp": stock.timestamp.isoformat(),
                    "last_price": stock.last_price,
                    "previous_close": stock.previous_close,
                    "volume": stock.volume,
                    "amount": stock.amount,
                    "turnover_rate": stock.turnover_rate,
                    "is_suspended": stock.is_suspended,
                    "limit_status": stock.limit_status.value,
                }
                for stock in sorted(
                    snapshot.stock_quotes.values(), key=lambda item: item.stock_code
                )
            ],
        }

    @staticmethod
    def _deserialize(payload: Dict[str, Any]) -> MarketSnapshot:
        if payload.get("schema_version") != JsonlSnapshotStore.SCHEMA_VERSION:
            raise ValueError("Unsupported snapshot schema version")
        timestamp = datetime.fromisoformat(payload["timestamp"])
        etf = payload["etf_quote"]
        stocks = {
            item["stock_code"]: StockQuote(
                timestamp=datetime.fromisoformat(item["timestamp"]),
                stock_code=item["stock_code"],
                last_price=item.get("last_price"),
                volume=float(item.get("volume", 0.0)),
                amount=float(item.get("amount", 0.0)),
                turnover_rate=item.get("turnover_rate"),
                is_suspended=bool(item.get("is_suspended", False)),
                limit_status=LimitStatus(item.get("limit_status", LimitStatus.NORMAL.value)),
                previous_close=JsonlSnapshotStore._optional_float(
                    item.get("previous_close")
                ),
            )
            for item in payload.get("stock_quotes", [])
        }
        return MarketSnapshot(
            timestamp=timestamp,
            etf_quote=ETFQuote(
                timestamp=timestamp,
                etf_code=etf["etf_code"],
                last_price=float(etf["last_price"]),
                bid_price=JsonlSnapshotStore._optional_float(etf.get("bid_price")),
                ask_price=JsonlSnapshotStore._optional_float(etf.get("ask_price")),
                volume=float(etf.get("volume", 0.0)),
                amount=float(etf.get("amount", 0.0)),
            ),
            stock_quotes=stocks,
        )

    @staticmethod
    def _optional_float(value: Any) -> Optional[float]:
        return None if value is None else float(value)

    @staticmethod
    def _fingerprint(payload: Dict[str, Any]) -> str:
        market_payload = dict(payload)
        market_payload.pop("captured_at", None)
        encoded = json.dumps(
            market_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _snapshot_identity(snapshot: MarketSnapshot) -> Tuple[Any, ...]:
        return (
            snapshot.timestamp,
            snapshot.etf_quote.etf_code,
            snapshot.etf_quote.last_price,
            snapshot.etf_quote.amount,
            tuple(
                (code, quote.timestamp, quote.last_price, quote.amount)
                for code, quote in sorted(snapshot.stock_quotes.items())
            ),
        )
