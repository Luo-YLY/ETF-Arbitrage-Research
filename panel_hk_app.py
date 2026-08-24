"""Independent Panel entry for mainland-listed cross-border ETF research."""

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from etf_arbitrage.dashboard_panel import build_cross_border_panel_dashboard


build_cross_border_panel_dashboard(ROOT).servable()
