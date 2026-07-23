"""Streamlit page entry for the existing mean-reversion monitor."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_arbitrage.dashboard.app import render


render()
