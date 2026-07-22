"""Replay recorded JSONL snapshots through research and backtest engines."""

import argparse
import json
from pathlib import Path
import sys

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_arbitrage.backtest import PremiumBacktester, ResearchReplay
from etf_arbitrage.config import BacktestConfig
from etf_arbitrage.data import (
    ComponentWeight,
    DataFrameReplayFeed,
    ETFInfo,
    JsonlSnapshotStore,
    SZSEPCFParser,
)
from etf_arbitrage.valuation import PCFIOPVCalculator


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay recorded ETF snapshots")
    parser.add_argument("--input", required=True, type=Path)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pcf", type=Path, help="SZSE PCF XML file")
    source.add_argument("--components-csv", type=Path)
    parser.add_argument("--etf-code")
    parser.add_argument("--name", default="创业板ETF")
    parser.add_argument("--tracking-index", default="创业板指")
    parser.add_argument("--shares", type=float, default=1.0)
    parser.add_argument("--creation-unit", type=int, default=1_000_000)
    parser.add_argument("--cash-component", type=float, default=0.0)
    parser.add_argument("--mode", choices=("indicative", "executable"), default="indicative")
    parser.add_argument("--entry-threshold", type=float, default=0.005)
    parser.add_argument("--exit-threshold", type=float, default=0.001)
    parser.add_argument("--max-holding-periods", type=int, default=30)
    parser.add_argument("--transaction-cost-bps", type=float, default=3.0)
    parser.add_argument("--output-dir", type=Path)
    return parser.parse_args()


def load_weights(path: Path, etf_code: str) -> list[ComponentWeight]:
    frame = pd.read_csv(path, dtype={"stock_code": str})
    missing = {"stock_code", "weight"} - set(frame.columns)
    if missing:
        raise ValueError("Component CSV is missing columns: {}".format(sorted(missing)))
    return [
        ComponentWeight(etf_code, str(row.stock_code).zfill(6), float(row.weight))
        for row in frame.itertuples(index=False)
    ]


def main() -> int:
    args = parse_args()
    pcf = SZSEPCFParser().parse(args.pcf) if args.pcf else None
    etf_code = args.etf_code or (pcf.etf_code if pcf else "159915")
    if pcf is not None and etf_code != pcf.etf_code:
        raise ValueError("--etf-code does not match PCF SecurityID")
    weights = pcf.component_weights() if pcf else load_weights(
        args.components_csv, etf_code
    )
    info = pcf.to_etf_info() if pcf else ETFInfo(
        etf_code=etf_code,
        name=args.name,
        exchange="SZSE",
        tracking_index=args.tracking_index,
        shares=args.shares,
        creation_unit=args.creation_unit,
        cash_component=args.cash_component,
    )
    etf_quotes, stock_quotes = JsonlSnapshotStore(args.input).to_frames(etf_code)
    if etf_quotes.empty:
        raise ValueError("No ETF snapshots found in {}".format(args.input))
    if stock_quotes.empty:
        raise ValueError("Recording contains no component snapshots; replay needs components")
    if pcf is not None:
        recording_days = set(pd.to_datetime(etf_quotes["timestamp"]).dt.date)
        if recording_days != {pcf.trading_day}:
            raise ValueError(
                "Recording dates {} do not match PCF TradingDay {}".format(
                    sorted(str(day) for day in recording_days), pcf.trading_day
                )
            )

    feed = DataFrameReplayFeed(info, weights, etf_quotes, stock_quotes)
    calculator = PCFIOPVCalculator(pcf) if pcf else None
    observations = ResearchReplay(feed, calculator=calculator).run(etf_code)
    backtest_input = observations.copy()
    if args.mode == "indicative":
        backtest_input["risk_blocked"] = backtest_input["indicative_risk_blocked"]
    result = PremiumBacktester(
        BacktestConfig(
            entry_threshold=args.entry_threshold,
            exit_threshold=args.exit_threshold,
            max_holding_periods=args.max_holding_periods,
            transaction_cost_bps=args.transaction_cost_bps,
            execution_mode=args.mode,
        )
    ).run(backtest_input)

    output_dir = args.output_dir or ROOT / "outputs" / "replay" / args.input.stem
    output_dir.mkdir(parents=True, exist_ok=True)
    observations.to_csv(output_dir / "observations.csv", index=False, encoding="utf-8-sig")
    result.timeline.to_csv(output_dir / "timeline.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame([trade.__dict__ for trade in result.trades]).to_csv(
        output_dir / "trades.csv", index=False, encoding="utf-8-sig"
    )
    summary = {
        "mode": args.mode,
        "iopv_model": "pcf_quantity_basket" if pcf else "normalized_weight",
        "pcf_trading_day": pcf.trading_day.isoformat() if pcf else None,
        "observations": len(observations),
        "closed_trades": len(result.trades),
        "total_return": result.performance.total_return,
        "sharpe": result.performance.sharpe,
        "max_drawdown": result.performance.max_drawdown,
        "win_rate": result.win_rate,
        "average_holding_periods": result.average_holding_periods,
    }
    (output_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("Outputs: {}".format(output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
