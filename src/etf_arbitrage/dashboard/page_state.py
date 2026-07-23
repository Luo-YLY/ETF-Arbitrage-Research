"""Shared lifecycle state for the two Streamlit dashboard pages."""

from __future__ import annotations

from typing import Any, MutableMapping


ACTIVE_PAGE_KEY = "_etf_dashboard_active_page"
MEAN_REVERSION_PAGE = "mean_reversion"
EXECUTABLE_ARBITRAGE_PAGE = "executable_arbitrage"


def activate_dashboard_page(
    session_state: MutableMapping[str, Any],
    page: str,
) -> None:
    session_state[ACTIVE_PAGE_KEY] = page
    if page == EXECUTABLE_ARBITRAGE_PAGE:
        return

    source = session_state.get("exec_source")
    if source is None:
        return
    try:
        running = bool(source.health().running)
    except Exception:
        running = True
    if running:
        try:
            source.stop()
        except Exception:
            pass


def dashboard_page_is_active(
    session_state: MutableMapping[str, Any],
    page: str,
) -> bool:
    return session_state.get(ACTIVE_PAGE_KEY) == page
