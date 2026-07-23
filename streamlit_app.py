"""Named Streamlit navigation for the two ETF research applications."""

from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent

navigation = st.navigation(
    [
        st.Page(
            ROOT / "pages" / "01_mean_reversion.py",
            title="实盘均值回复监控",
            default=True,
        ),
        st.Page(
            ROOT / "pages" / "02_executable_arbitrage.py",
            title="实盘套利模拟",
        ),
    ],
    position="sidebar",
)
navigation.run()
