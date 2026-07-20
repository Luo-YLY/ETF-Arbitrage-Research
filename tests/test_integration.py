from etf_arbitrage.backtest import PremiumBacktester, ResearchReplay
from etf_arbitrage.data import SyntheticDataFeed


def test_synthetic_replay_covers_signal_risk_and_backtest() -> None:
    feed = SyntheticDataFeed("159919", periods=180)
    observations = ResearchReplay(feed).run("159919")
    result = PremiumBacktester().run(observations)

    assert len(observations) == 180
    assert observations["iopv"].notna().all()
    assert (observations["suspension_ratio"] > 0).any()
    assert observations["risk_blocked"].any()
    assert ((observations["signal"] != "none") & observations["signal_allowed"]).any()
    assert len(result.trades) >= 1
