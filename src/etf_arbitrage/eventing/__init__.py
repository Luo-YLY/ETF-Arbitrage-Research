"""Lightweight local event infrastructure for research and paper simulation."""

from .adapters import SnapshotEventAdapter
from .audit import EventAuditIndex
from .bus import EventBus, EventPublisher, EventSubscriber, InMemoryEventBus
from .dependency_graph import ETFDependencyGraph
from .market_clock import MarketSessionEventGenerator
from .opportunity_engine import (
    EventDrivenOpportunityEngine,
    OpportunityProjectionResult,
)
from .pipeline import EventDrivenResearchPipeline, PipelineIngestResult
from .projectors import PaperOutcomeProjector, ResearchOutcomeProjector
from .replay import EventReplay, ReplayResult
from .serialization import EventSerializer
from .snapshot_assembler import (
    SnapshotAssembler,
    SnapshotAssemblyResult,
    SnapshotAssemblyStatus,
    order_book_from_payload,
    order_book_to_payload,
)
from .store import EventStore, FileEventStore

__all__ = [
    "ETFDependencyGraph",
    "EventAuditIndex",
    "EventBus",
    "EventDrivenOpportunityEngine",
    "EventDrivenResearchPipeline",
    "EventPublisher",
    "EventReplay",
    "EventSerializer",
    "EventStore",
    "EventSubscriber",
    "FileEventStore",
    "InMemoryEventBus",
    "MarketSessionEventGenerator",
    "OpportunityProjectionResult",
    "PaperOutcomeProjector",
    "PipelineIngestResult",
    "ReplayResult",
    "ResearchOutcomeProjector",
    "SnapshotEventAdapter",
    "SnapshotAssembler",
    "SnapshotAssemblyResult",
    "SnapshotAssemblyStatus",
    "order_book_from_payload",
    "order_book_to_payload",
]
