"""Run the full synthetic replay and print a concise research summary."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_arbitrage.backtest import PremiumBacktester, ResearchReplay
from etf_arbitrage.data import SyntheticDataFeed


def main() -> None:
    feed = SyntheticDataFeed("159919")
    observations = ResearchReplay(feed).run("159919")
    result = PremiumBacktester().run(observations)
    print(observations.tail(5).to_string(index=False))
    print("\nclosed trades:", len(result.trades))
    print("total return: {:.3%}".format(result.performance.total_return))
    print("sharpe: {:.2f}".format(result.performance.sharpe))
    print("max drawdown: {:.3%}".format(result.performance.max_drawdown))
    print("win rate: {:.1%}".format(result.win_rate))


if __name__ == "__main__":
    main()
