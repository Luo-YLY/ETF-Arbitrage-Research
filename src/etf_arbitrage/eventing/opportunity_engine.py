"""Bridge assembled snapshots to the existing executable research detector."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from etf_arbitrage.arbitrage import ArbitrageEvaluation, ExecutableArbitrageDetector
from etf_arbitrage.data.pcf import PCFDocument
from etf_arbitrage.domain import EventEnvelope, EventType, InstrumentId
from etf_arbitrage.executable_config import CostConfig, ExecutionConfig

from .projectors import ResearchOutcomeProjector
from .snapshot_assembler import SnapshotAssemblyResult


@dataclass(frozen=True)
class OpportunityProjectionResult:
    evaluation: Optional[ArbitrageEvaluation]
    events: tuple[EventEnvelope, ...]
    skipped_reason: Optional[str] = None


class EventDrivenOpportunityEngine:
    """Evaluate only assembled snapshots; never receives raw vendor events."""

    def __init__(
        self,
        *,
        execution: ExecutionConfig = ExecutionConfig(),
        costs: CostConfig = CostConfig(),
        projector: Optional[ResearchOutcomeProjector] = None,
    ) -> None:
        self.execution = execution
        self.costs = costs
        self.projector = projector or ResearchOutcomeProjector()
        self._pcfs: dict[InstrumentId, PCFDocument] = {}
        self._detectors: dict[tuple[InstrumentId, str], ExecutableArbitrageDetector] = {}

    def register_pcf(self, pcf: PCFDocument) -> None:
        self._pcfs[pcf.etf_id] = pcf
        self._detectors[(pcf.etf_id, pcf.event_version)] = (
            ExecutableArbitrageDetector(
                pcf,
                execution=self.execution,
                costs=self.costs,
            )
        )

    def process(
        self,
        result: SnapshotAssemblyResult,
        trigger: EventEnvelope,
    ) -> OpportunityProjectionResult:
        self._refresh_pcf_from_trigger(trigger)
        if result.snapshot is None:
            return OpportunityProjectionResult(
                evaluation=None,
                events=(),
                skipped_reason="SNAPSHOT_REJECTED",
            )
        pcf = self._pcfs.get(result.etf_id)
        if pcf is None:
            return OpportunityProjectionResult(
                evaluation=None,
                events=(),
                skipped_reason="PCF_NOT_REGISTERED_WITH_OPPORTUNITY_ENGINE",
            )
        if result.pcf_version != pcf.event_version:
            return OpportunityProjectionResult(
                evaluation=None,
                events=(),
                skipped_reason="PCF_VERSION_MISMATCH",
            )
        detector = self._detectors[(result.etf_id, pcf.event_version)]
        evaluation = detector.evaluate(result.snapshot)
        return OpportunityProjectionResult(
            evaluation=evaluation,
            events=self.projector.project(evaluation, trigger),
        )

    def project(
        self,
        result: SnapshotAssemblyResult,
        trigger: EventEnvelope,
    ) -> tuple[EventEnvelope, ...]:
        """Pipeline-compatible projection hook."""

        return self.process(result, trigger).events

    def reset(self) -> None:
        self.projector.reset()

    def _refresh_pcf_from_trigger(self, trigger: EventEnvelope) -> None:
        if trigger.event_type not in {
            EventType.PCF_VALIDATED,
            EventType.PCF_REVISED,
        }:
            return
        payload = trigger.payload.get("pcf", trigger.payload)
        if not isinstance(payload, Mapping):
            raise ValueError("PCF event payload must contain a PCF mapping")
        pcf = PCFDocument.from_dict(payload)
        if pcf.etf_id != trigger.instrument_id:
            raise ValueError("PCF event instrument does not match PCF ETF")
        self.register_pcf(pcf)
