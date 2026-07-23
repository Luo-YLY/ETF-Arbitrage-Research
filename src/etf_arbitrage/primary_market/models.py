"""Paper primary-market request models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Optional, Tuple


class PrimaryMarketStatus(str, Enum):
    REQUEST_CREATED = "REQUEST_CREATED"
    REQUEST_SUBMITTED = "REQUEST_SUBMITTED"
    REQUEST_ACCEPTED = "REQUEST_ACCEPTED"
    REQUEST_CONFIRMED = "REQUEST_CONFIRMED"
    REQUEST_REJECTED = "REQUEST_REJECTED"
    FINAL_CASH_SETTLED = "FINAL_CASH_SETTLED"


@dataclass(frozen=True)
class PrimaryMarketRequest:
    request_id: str
    cycle_id: str
    etf_code: str
    direction: str
    cu_count: int
    submit_time: datetime
    confirm_time: Optional[datetime]
    final_settlement_time: Optional[datetime]
    pcf_date: date
    estimated_cash_component: float
    final_cash_component: float
    status: PrimaryMarketStatus
    reject_reason: Optional[str]
    status_history: Tuple[str, ...]
