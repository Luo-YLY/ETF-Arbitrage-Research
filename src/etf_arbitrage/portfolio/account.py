"""Conservative paper-account resource reservation and cycle settlement."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional

from etf_arbitrage.arbitrage.models import DirectionEvaluation
from etf_arbitrage.executable_config import AccountConfig, ArbitrageDirection, ExecutionMode


@dataclass(frozen=True)
class ResourceCheck:
    allowed: bool
    reason: Optional[str]
    required_cash: float
    required_etf_borrow: float


@dataclass(frozen=True)
class LedgerEntry:
    timestamp: datetime
    cycle_id: str
    entry_type: str
    cash_change: float
    realized_pnl: float
    note: str


@dataclass
class AccountState:
    config: AccountConfig = field(default_factory=AccountConfig)
    cash: float = field(init=False)
    realized_pnl: float = 0.0
    daily_cu: int = 0
    etf_inventory: Dict[str, float] = field(default_factory=dict)
    ledger: list[LedgerEntry] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.cash = float(self.config.initial_cash)

    def resource_check(
        self,
        etf_code: str,
        result: DirectionEvaluation,
        mode: ExecutionMode,
    ) -> ResourceCheck:
        if result.direction == ArbitrageDirection.CREATION:
            required_cash = max(0.0, result.basket.total_value + result.estimated_costs)
            etf_quantity = result.etf_sweep.requested_quantity
            available_etf = self.etf_inventory.get(etf_code, self.config.initial_etf_inventory)
            required_borrow = max(0.0, etf_quantity - available_etf) if mode == ExecutionMode.INVENTORY_LOCKED else 0.0
            if required_borrow > self.config.etf_borrow_limit:
                return ResourceCheck(False, "ETF_BORROW_LIMIT", required_cash, required_borrow)
        else:
            required_cash = max(0.0, result.etf_sweep.total_value + result.estimated_costs)
            required_borrow = 0.0
            if mode == ExecutionMode.INVENTORY_LOCKED and not self.config.allow_stock_borrow:
                return ResourceCheck(False, "STOCK_BORROW_DISABLED", required_cash, 0.0)
        if required_cash > self.cash:
            return ResourceCheck(False, "INSUFFICIENT_CASH", required_cash, required_borrow)
        return ResourceCheck(True, None, required_cash, required_borrow)

    def settle_cycle(
        self,
        timestamp: datetime,
        cycle_id: str,
        etf_code: str,
        cu_count: int,
        realized_pnl: float,
        note: str,
    ) -> None:
        self.cash += realized_pnl
        self.realized_pnl += realized_pnl
        self.daily_cu += cu_count
        self.ledger.append(
            LedgerEntry(
                timestamp=timestamp,
                cycle_id=cycle_id,
                entry_type="CYCLE_SETTLEMENT",
                cash_change=realized_pnl,
                realized_pnl=realized_pnl,
                note="{}; inventory reconciled for {}".format(note, etf_code),
            )
        )
