"""Opportunity-to-settlement paper engine with no real-order capability."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Optional
from uuid import uuid4

from etf_arbitrage.arbitrage import ArbitrageEvaluation, ExecutableArbitrageDetector
from etf_arbitrage.data.pcf import PCFDocument
from etf_arbitrage.executable_config import (
    ArbitrageDirection,
    ExecutionMode,
    PaperArbitrageConfig,
)
from etf_arbitrage.execution import CycleState, PaperExecutionSimulator, PaperTradeCycle
from etf_arbitrage.market_data import DataQualityChecker, MarketSnapshot
from etf_arbitrage.portfolio import AccountState
from etf_arbitrage.primary_market import PrimaryMarketEmulator, PrimaryMarketRequest, PrimaryMarketStatus


@dataclass(frozen=True)
class PaperEngineResult:
    decision_evaluation: ArbitrageEvaluation
    fill_evaluation: Optional[ArbitrageEvaluation]
    cycle: Optional[PaperTradeCycle]
    primary_request: Optional[PrimaryMarketRequest]


class PaperArbitrageEngine:
    """Creates only in-memory simulated orders; no broker adapter exists here."""

    def __init__(self, pcf: PCFDocument, config: PaperArbitrageConfig) -> None:
        self.pcf = pcf
        self.config = config
        checker = DataQualityChecker(config.quality)
        self.detector = ExecutableArbitrageDetector(
            pcf, config.execution, config.costs, checker
        )
        self.account = AccountState(config.account)
        self.execution_simulator = PaperExecutionSimulator()
        self.primary = PrimaryMarketEmulator(
            confirmation_latency_ms=config.execution.primary_market_latency_ms,
            rejection_probability=config.primary_market.rejection_probability,
            cash_component_error=config.primary_market.cash_component_error,
            final_cash_delay_ms=config.primary_market.final_cash_delay_ms,
            random_seed=config.simulation.random_seed,
        )
        self.opportunities: list[ArbitrageEvaluation] = []
        self.cycles: list[PaperTradeCycle] = []
        self.primary_requests: list[PrimaryMarketRequest] = []

    def process(
        self,
        decision_snapshot: MarketSnapshot,
        fill_snapshot: Optional[MarketSnapshot] = None,
    ) -> PaperEngineResult:
        decision = self.detector.evaluate(decision_snapshot)
        self.opportunities.append(decision)
        candidate = self._best_candidate(decision)
        if candidate is None:
            return PaperEngineResult(decision, None, None, None)
        if self.config.execution.record_opportunities_only or not self.config.execution.auto_paper_trade:
            return PaperEngineResult(decision, None, None, None)

        cycle_id = "cycle-{}".format(uuid4().hex[:12])
        states = [CycleState.IDLE.value, CycleState.CANDIDATE_DETECTED.value]
        required_fill_time = decision_snapshot.snapshot_timestamp + timedelta(
            milliseconds=(
                self.config.execution.decision_latency_ms
                + self.config.execution.order_latency_ms
            )
        )
        if fill_snapshot is None or fill_snapshot.snapshot_timestamp < required_fill_time:
            cycle = self._rejected_cycle(
                cycle_id,
                candidate.direction,
                decision_snapshot,
                states,
                "NO_POST_LATENCY_SNAPSHOT",
                candidate.net_profit,
            )
            self.cycles.append(cycle)
            return PaperEngineResult(decision, None, cycle, None)

        fill_evaluation = self.detector.evaluate(fill_snapshot)
        fill_result = (
            fill_evaluation.creation
            if candidate.direction == ArbitrageDirection.CREATION
            else fill_evaluation.redemption
        )
        states.append(CycleState.PRE_TRADE_CHECK.value)
        if not fill_result.executable:
            cycle = self._rejected_cycle(
                cycle_id,
                candidate.direction,
                decision_snapshot,
                states,
                "DELAYED_MARKET_REJECTED:{}".format(",".join(fill_result.rejection_reasons)),
                candidate.net_profit,
                fill_snapshot.snapshot_timestamp,
            )
            self.cycles.append(cycle)
            return PaperEngineResult(decision, fill_evaluation, cycle, None)

        resources = self.account.resource_check(
            self.pcf.etf_code, fill_result, self.config.execution.mode
        )
        if self.account.daily_cu + fill_result.cu_count > self.config.execution.maximum_daily_cu:
            resources = type(resources)(False, "MAXIMUM_DAILY_CU", resources.required_cash, resources.required_etf_borrow)
        if not resources.allowed:
            cycle = self._rejected_cycle(
                cycle_id,
                candidate.direction,
                decision_snapshot,
                states,
                resources.reason or "RESOURCE_REJECTED",
                candidate.net_profit,
                fill_snapshot.snapshot_timestamp,
            )
            self.cycles.append(cycle)
            return PaperEngineResult(decision, fill_evaluation, cycle, None)

        states.extend(
            [
                CycleState.RESOURCES_RESERVED.value,
                CycleState.LEGS_SUBMITTED.value,
                CycleState.FULLY_FILLED.value,
            ]
        )
        orders, fills = self.execution_simulator.build(
            cycle_id, fill_result, self.pcf, fill_snapshot.snapshot_timestamp
        )
        states.append(CycleState.PRIMARY_MARKET_SUBMITTED.value)
        request = self.primary.submit(
            cycle_id,
            self.pcf,
            candidate.direction,
            fill_result.cu_count,
            fill_snapshot.snapshot_timestamp,
        )
        self.primary_requests.append(request)
        if request.status == PrimaryMarketStatus.REQUEST_REJECTED:
            states.extend(
                [CycleState.PRIMARY_MARKET_REJECTED.value, CycleState.ABORT_AND_UNWIND.value]
            )
            cycle = PaperTradeCycle(
                cycle_id=cycle_id,
                direction=candidate.direction.value,
                decision_time=decision_snapshot.snapshot_timestamp,
                fill_time=fill_snapshot.snapshot_timestamp,
                state=CycleState.ABORT_AND_UNWIND,
                state_history=tuple(states),
                orders=orders,
                fills=fills,
                primary_request_id=request.request_id,
                snapshot_profit=candidate.net_profit,
                execution_profit=fill_result.gross_profit - fill_result.estimated_costs,
                final_pnl=-fill_result.estimated_costs,
                rejection_reason=request.reject_reason,
                execution_risk_label=self._risk_label(),
            )
            self.cycles.append(cycle)
            return PaperEngineResult(decision, fill_evaluation, cycle, request)

        states.extend(
            [
                CycleState.PRIMARY_MARKET_CONFIRMED.value,
                CycleState.INVENTORY_RECONCILED.value,
                CycleState.CLOSED.value,
            ]
        )
        execution_profit = fill_result.gross_profit - fill_result.estimated_costs
        cash_error = (
            request.final_cash_component - request.estimated_cash_component
        ) * fill_result.cu_count
        final_pnl = (
            execution_profit - cash_error
            if candidate.direction == ArbitrageDirection.CREATION
            else execution_profit + cash_error
        )
        settlement_time = request.final_settlement_time or fill_snapshot.snapshot_timestamp
        self.account.settle_cycle(
            settlement_time,
            cycle_id,
            self.pcf.etf_code,
            fill_result.cu_count,
            final_pnl,
            candidate.direction.value,
        )
        cycle = PaperTradeCycle(
            cycle_id=cycle_id,
            direction=candidate.direction.value,
            decision_time=decision_snapshot.snapshot_timestamp,
            fill_time=fill_snapshot.snapshot_timestamp,
            state=CycleState.CLOSED,
            state_history=tuple(states),
            orders=orders,
            fills=fills,
            primary_request_id=request.request_id,
            snapshot_profit=candidate.net_profit,
            execution_profit=execution_profit,
            final_pnl=final_pnl,
            rejection_reason=None,
            execution_risk_label=self._risk_label(),
        )
        self.cycles.append(cycle)
        return PaperEngineResult(decision, fill_evaluation, cycle, request)

    @staticmethod
    def _best_candidate(evaluation: ArbitrageEvaluation):
        candidates = [
            item for item in (evaluation.creation, evaluation.redemption) if item.executable
        ]
        return max(candidates, key=lambda item: item.net_profit) if candidates else None

    def _rejected_cycle(
        self,
        cycle_id,
        direction,
        decision_snapshot,
        states,
        reason,
        snapshot_profit,
        fill_time=None,
    ) -> PaperTradeCycle:
        states.append(CycleState.REJECTED.value)
        return PaperTradeCycle(
            cycle_id=cycle_id,
            direction=direction.value,
            decision_time=decision_snapshot.snapshot_timestamp,
            fill_time=fill_time,
            state=CycleState.REJECTED,
            state_history=tuple(states),
            orders=(),
            fills=(),
            primary_request_id=None,
            snapshot_profit=snapshot_profit,
            execution_profit=0.0,
            final_pnl=0.0,
            rejection_reason=reason,
            execution_risk_label=self._risk_label(),
        )

    def _risk_label(self) -> str:
        if self.config.execution.mode == ExecutionMode.INVENTORY_LOCKED:
            return "INVENTORY_LOCKED_APPROXIMATE_LOCKED_ARBITRAGE"
        return "SEQUENTIAL_NO_BORROW_MARKET_EXPOSURE"
