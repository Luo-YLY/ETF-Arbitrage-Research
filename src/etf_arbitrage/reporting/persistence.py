"""Reproducible CSV/Parquet/JSON run artifacts for the paper simulator."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from uuid import uuid4

import pandas as pd


class RunRecorder:
    TABLES = (
        "snapshots",
        "opportunities",
        "rejected_opportunities",
        "orders",
        "fills",
        "primary_market_requests",
        "trades",
        "pnl",
        "data_quality_events",
    )

    def __init__(self, root: Path | str, config: Mapping[str, Any], run_id: Optional[str] = None) -> None:
        self.run_id = run_id or "run-{}".format(uuid4().hex[:12])
        self.root = Path(root) / self.run_id
        self.config = dict(config)
        self.rows: Dict[str, list[dict]] = {name: [] for name in self.TABLES}

    def record(self, table: str, value: Any) -> None:
        if table not in self.rows:
            raise KeyError("Unknown run table: {}".format(table))
        normalized = self._normalize(asdict(value) if is_dataclass(value) else dict(value))
        self.rows[table].append(normalized)

    def save(self, summary: Optional[Mapping[str, Any]] = None) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        self._write_json(self.root / "config.json", self.config)
        for table, rows in self.rows.items():
            frame = pd.DataFrame(rows)
            frame.to_csv(self.root / "{}.csv".format(table), index=False, encoding="utf-8-sig")
            try:
                frame.to_parquet(self.root / "{}.parquet".format(table), index=False)
            except ImportError:
                pass
        self._write_json(
            self.root / "run_summary.json",
            dict(summary or {}, run_id=self.run_id, tables={key: len(value) for key, value in self.rows.items()}),
        )
        return self.root

    @classmethod
    def _normalize(cls, value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if is_dataclass(value):
            return cls._normalize(asdict(value))
        if isinstance(value, dict):
            return {str(key): cls._normalize(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._normalize(item) for item in value]
        return value

    @classmethod
    def _write_json(cls, path: Path, value: Any) -> None:
        path.write_text(
            json.dumps(cls._normalize(value), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
