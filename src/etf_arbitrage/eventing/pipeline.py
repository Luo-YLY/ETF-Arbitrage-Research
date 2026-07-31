"""Composable event-driven research pipeline.

The pipeline intentionally stops at event projection and snapshot assembly. It
does not submit orders or connect to a broker, keeping the first-stage design
inside the project's paper-research boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from etf_arbitrage.domain.events import EventEnvelope

from .bus import EventBus
from .snapshot_assembler import SnapshotAssembler, SnapshotAssemblyResult
from .store import EventStore


SnapshotProjector = Callable[
    [SnapshotAssemblyResult, EventEnvelope],
    Iterable[EventEnvelope],
]


@dataclass(frozen=True, slots=True)
class PipelineIngestResult:
    """Observable result of ingesting one source event."""

    stored: bool
    published: bool
    assembly_results: tuple[SnapshotAssemblyResult, ...]
    outcome_events: tuple[EventEnvelope, ...]


class EventDrivenResearchPipeline:
    """Persist, publish, and project source events in deterministic order."""

    def __init__(
        self,
        *,
        event_store: EventStore,
        event_bus: EventBus,
        snapshot_assembler: SnapshotAssembler,
        snapshot_projector: Optional[SnapshotProjector] = None,
    ) -> None:
        self._event_store = event_store
        self._event_bus = event_bus
        self._snapshot_assembler = snapshot_assembler
        self._snapshot_projector = snapshot_projector

    def ingest(
        self,
        event: EventEnvelope,
        *,
        persist: bool = True,
    ) -> PipelineIngestResult:
        """Ingest one source event.

        Persistent duplicates are treated as already processed. Replay callers
        can set ``persist=False`` to project historical events without appending
        them again.
        """

        stored = self._event_store.append(event) if persist else False
        if persist and not stored:
            return PipelineIngestResult(
                stored=False,
                published=False,
                assembly_results=(),
                outcome_events=(),
            )

        published = self._event_bus.publish(event)
        assembly_results = self._snapshot_assembler.process(event)
        outcome_events: list[EventEnvelope] = [
            result.to_event(event) for result in assembly_results
        ]
        if self._snapshot_projector is not None:
            for result in assembly_results:
                outcome_events.extend(self._snapshot_projector(result, event))

        for outcome in outcome_events:
            self.emit(outcome, persist=persist)

        return PipelineIngestResult(
            stored=stored,
            published=published,
            assembly_results=assembly_results,
            outcome_events=tuple(outcome_events),
        )

    def ingest_many(
        self,
        events: Iterable[EventEnvelope],
        *,
        persist: bool = True,
    ) -> tuple[PipelineIngestResult, ...]:
        return tuple(
            self.ingest(event, persist=persist) for event in events
        )

    def emit(self, event: EventEnvelope, *, persist: bool = True) -> bool:
        """Persist and publish a derived event without re-projecting it."""

        if persist:
            self._event_store.append(event)
        return self._event_bus.publish(event)

    def publish(self, event: EventEnvelope) -> bool:
        """Allow ``EventReplay`` to replay source events through this pipeline."""

        return self.ingest(event, persist=False).published

    def reset_projection(self) -> None:
        """Reset stateful projections before a deterministic full replay."""

        self._snapshot_assembler.reset()
        reset_bus = getattr(self._event_bus, "reset_deduplication", None)
        if callable(reset_bus):
            reset_bus()
        projector_owner = getattr(
            self._snapshot_projector, "__self__", self._snapshot_projector
        )
        reset_projector = getattr(projector_owner, "reset", None)
        if callable(reset_projector):
            reset_projector()
