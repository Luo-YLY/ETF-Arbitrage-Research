"""Event-driven research primitives; no real execution capability."""

from .pcf_diff import (
    PCFChange,
    PCFDiffResult,
    compare_pcf,
    validated_pcf_event,
)

__all__ = [
    "PCFChange",
    "PCFDiffResult",
    "compare_pcf",
    "validated_pcf_event",
]
