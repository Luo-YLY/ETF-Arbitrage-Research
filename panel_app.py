"""Panel entry point for the incremental ETF market replay console."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from etf_arbitrage.dashboard_panel import build_panel_dashboard


build_panel_dashboard(ROOT).servable()
