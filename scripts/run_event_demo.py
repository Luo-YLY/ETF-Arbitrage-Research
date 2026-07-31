"""Run the first-stage event pipeline with the existing deterministic feed."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from etf_arbitrage.eventing import (  # noqa: E402
    EventDrivenOpportunityEngine,
    EventDrivenResearchPipeline,
    FileEventStore,
    InMemoryEventBus,
    SnapshotEventAdapter,
    SnapshotAssembler,
)
from etf_arbitrage.exchanges import SZSEAdapter  # noqa: E402
from etf_arbitrage.executable_config import SimulationConfig  # noqa: E402
from etf_arbitrage.market_data import SimulatedMarketDataSource  # noqa: E402
from etf_arbitrage.strategies.event_driven import validated_pcf_event  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay the existing SZSE simulator through the local event pipeline."
    )
    parser.add_argument(
        "--pcf",
        type=Path,
        default=PROJECT_ROOT
        / "data"
        / "pcf"
        / "20260722"
        / "pcf_159915_20260722.xml",
    )
    parser.add_argument("--ticks", type=int, default=3)
    parser.add_argument(
        "--event-root",
        type=Path,
        default=PROJECT_ROOT / "data" / "events",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.ticks <= 0:
        raise ValueError("--ticks must be positive")

    adapter = SZSEAdapter()
    pcf = adapter.pcf_parser.parse(args.pcf)
    source = SimulatedMarketDataSource(
        pcf,
        SimulationConfig(total_ticks=args.ticks),
    )
    opportunity_engine = EventDrivenOpportunityEngine()
    opportunity_engine.register_pcf(pcf)
    snapshot_adapter = SnapshotEventAdapter.from_pcf(pcf)
    pipeline = EventDrivenResearchPipeline(
        event_store=FileEventStore(args.event_root),
        event_bus=InMemoryEventBus(),
        snapshot_assembler=SnapshotAssembler(),
        snapshot_projector=opportunity_engine.project,
    )

    source.start()
    status_counts: dict[str, int] = {}
    event_counts: dict[str, int] = {}
    try:
        for tick in range(args.ticks):
            snapshot = source.step()
            if tick == 0:
                pcf_event = validated_pcf_event(
                    pcf,
                    event_time=snapshot.snapshot_timestamp,
                    received_time=snapshot.etf_order_book.receive_timestamp,
                    source="szse_pcf_parser",
                )
                pipeline.ingest(pcf_event)

            for source_event in snapshot_adapter.to_events(
                snapshot,
                trade_date=pcf.trading_day,
            ):
                result = pipeline.ingest(source_event)
                for assembled in result.assembly_results:
                    key = assembled.status.value
                    status_counts[key] = status_counts.get(key, 0) + 1
                for outcome in result.outcome_events:
                    key = outcome.event_type.value
                    event_counts[key] = event_counts.get(key, 0) + 1
    finally:
        source.stop()
        source.disconnect()

    print("ETF:", pcf.etf_id.key)
    print("PCF version:", pcf.event_version)
    print("Ticks:", args.ticks)
    print("Assembly outcomes:", status_counts)
    print("Projected events:", event_counts)
    print("Event store:", args.event_root.resolve())
    print("Research mode only; no broker or real-order interface is connected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
