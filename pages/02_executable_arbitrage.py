"""Streamlit multipage entry for the executable-arbitrage simulator."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_arbitrage.dashboard.executable_app import render


render()
