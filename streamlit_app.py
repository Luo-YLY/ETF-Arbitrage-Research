"""Streamlit entry point that works without an editable install."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from etf_arbitrage.dashboard.app import render


render()
