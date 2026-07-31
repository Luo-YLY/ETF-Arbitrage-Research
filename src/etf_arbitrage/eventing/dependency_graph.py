"""Atomic component-to-ETF dependency graph for incremental recomputation."""

from __future__ import annotations

from threading import RLock
from typing import Iterable, Optional

from etf_arbitrage.domain import InstrumentId


class ETFDependencyGraph:
    def __init__(self) -> None:
        self._components_by_etf: dict[InstrumentId, frozenset[InstrumentId]] = {}
        self._etfs_by_component: dict[InstrumentId, set[InstrumentId]] = {}
        self._pcf_versions: dict[InstrumentId, str] = {}
        self._lock = RLock()

    def replace_etf(
        self,
        etf_id: InstrumentId,
        components: Iterable[InstrumentId],
        pcf_version: str,
    ) -> None:
        """Replace one ETF dependency set without exposing a mixed PCF state."""

        new_components = frozenset(components)
        with self._lock:
            old_components = self._components_by_etf.get(etf_id, frozenset())
            new_reverse = {
                component: set(etfs)
                for component, etfs in self._etfs_by_component.items()
            }
            for component in old_components - new_components:
                linked = new_reverse.get(component)
                if linked is not None:
                    linked.discard(etf_id)
                    if not linked:
                        new_reverse.pop(component, None)
            for component in new_components:
                new_reverse.setdefault(component, set()).add(etf_id)
            self._components_by_etf = dict(self._components_by_etf)
            self._components_by_etf[etf_id] = new_components
            self._etfs_by_component = new_reverse
            self._pcf_versions[etf_id] = str(pcf_version)

    def remove_etf(self, etf_id: InstrumentId) -> None:
        with self._lock:
            old_components = self._components_by_etf.pop(etf_id, frozenset())
            for component in old_components:
                linked = self._etfs_by_component.get(component)
                if linked is None:
                    continue
                linked.discard(etf_id)
                if not linked:
                    self._etfs_by_component.pop(component, None)
            self._pcf_versions.pop(etf_id, None)

    def clear(self) -> None:
        """Discard all projected dependencies before a clean replay."""

        with self._lock:
            self._components_by_etf.clear()
            self._etfs_by_component.clear()
            self._pcf_versions.clear()

    def affected_etfs(self, instrument_id: InstrumentId) -> frozenset[InstrumentId]:
        with self._lock:
            affected = set(self._etfs_by_component.get(instrument_id, set()))
            if instrument_id in self._components_by_etf:
                affected.add(instrument_id)
            return frozenset(affected)

    def components_for(self, etf_id: InstrumentId) -> frozenset[InstrumentId]:
        with self._lock:
            return self._components_by_etf.get(etf_id, frozenset())

    def pcf_version(self, etf_id: InstrumentId) -> Optional[str]:
        with self._lock:
            return self._pcf_versions.get(etf_id)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "components_by_etf": {
                    etf.key: sorted(component.key for component in components)
                    for etf, components in self._components_by_etf.items()
                },
                "pcf_versions": {
                    etf.key: version for etf, version in self._pcf_versions.items()
                },
            }
