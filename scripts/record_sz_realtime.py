"""Poll Shenzhen Redis quotations and persist deduplicated intraday snapshots."""

import argparse
from datetime import date
from pathlib import Path
import sys
import time
from typing import Optional

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_arbitrage.backtest import ResearchReplay
from etf_arbitrage.data import (
    ComponentWeight,
    ETFInfo,
    JsonlSnapshotStore,
    SZRedisDataFeed,
    SZRedisQuotationClient,
    SZRedisSettings,
    SZSEPCFParser,
)
from etf_arbitrage.valuation import PCFIOPVCalculator


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record live Shenzhen ETF snapshots")
    parser.add_argument("--etf-code")
    parser.add_argument("--trade-date", help="Redis date key in YYYYMMDD format")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--pcf", type=Path, help="SZSE PCF XML file")
    source.add_argument("--components-csv", type=Path)
    parser.add_argument("--name", default="创业板ETF")
    parser.add_argument("--tracking-index", default="创业板指")
    parser.add_argument("--shares", type=float, default=1.0)
    parser.add_argument("--creation-unit", type=int, default=1_000_000)
    parser.add_argument("--cash-component", type=float, default=0.0)
    parser.add_argument("--redis-code-suffix", default=".SZ")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--max-polls", type=int)
    parser.add_argument("--require-bid-ask", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def load_weights(path: Optional[Path], etf_code: str) -> list[ComponentWeight]:
    if path is None:
        return []
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
    if args.interval <= 0:
        raise ValueError("interval must be positive")
    pcf = SZSEPCFParser().parse(args.pcf) if args.pcf else None
    etf_code = args.etf_code or (pcf.etf_code if pcf else "159915")
    if pcf is not None and etf_code != pcf.etf_code:
        raise ValueError("--etf-code does not match PCF SecurityID")
    pcf_trade_date = pcf.trading_day.strftime("%Y%m%d") if pcf else None
    if args.trade_date and pcf_trade_date and args.trade_date != pcf_trade_date:
        raise ValueError("--trade-date does not match PCF TradingDay")
    trade_date = args.trade_date or pcf_trade_date or date.today().strftime("%Y%m%d")
    output = args.output or (
        ROOT / "tmp" / "recordings" / trade_date / "{}.jsonl".format(etf_code)
    )
    weights = pcf.component_weights() if pcf else load_weights(args.components_csv, etf_code)
    info = pcf.to_etf_info() if pcf else ETFInfo(
        etf_code=etf_code,
        name=args.name,
        exchange="SZSE",
        tracking_index=args.tracking_index,
        shares=args.shares,
        creation_unit=args.creation_unit,
        cash_component=args.cash_component,
    )
    client = SZRedisQuotationClient(SZRedisSettings.from_env())
    client.ping()
    feed = SZRedisDataFeed(
        client=client,
        etf_info=info,
        weights=weights,
        trade_date=trade_date,
        redis_code_suffix=args.redis_code_suffix,
        require_bid_ask=args.require_bid_ask,
    )
    store = JsonlSnapshotStore(output)
    calculator = PCFIOPVCalculator(pcf) if pcf else None
    research = ResearchReplay(feed, calculator=calculator)

    print("Recording {} to {}".format(etf_code, output))
    if pcf:
        print(
            "PCF {}: {} components, creation unit {}, estimated cash {:.2f}".format(
                pcf.trading_day,
                len(pcf.components),
                pcf.creation_redemption_unit,
                pcf.estimate_cash_component,
            )
        )
    if not weights:
        print("ETF-only mode: snapshots are stored, but IOPV is not calculated.")
    elif not args.require_bid_ask:
        print("Indicative mode: Premium uses last price; executable signal is disabled.")

    polls = 0
    try:
        while args.max_polls is None or polls < args.max_polls:
            started = time.monotonic()
            snapshots = list(feed.snapshots(etf_code))
            polls += 1
            if not snapshots:
                print("poll={} no Redis record".format(polls))
            else:
                snapshot = snapshots[0]
                stored = store.append(snapshot)
                if stored and weights:
                    row = research.process_snapshot(snapshot, info, weights)
                    print(
                        "{} price={:.4f} iopv={:.4f} premium={:.4%} "
                        "signal={} allowed={} risk={}".format(
                            row["timestamp"],
                            row["etf_price"],
                            row["iopv"],
                            row["premium"],
                            row["signal"],
                            row["signal_allowed"],
                            row["risk_blockers"] or "none",
                        )
                    )
                elif stored:
                    print(
                        "{} price={:.4f} stored ETF-only snapshot".format(
                            snapshot.timestamp, snapshot.etf_quote.last_price
                        )
                    )
                else:
                    print("{} unchanged; skipped".format(snapshot.timestamp))
            remaining = args.interval - (time.monotonic() - started)
            if remaining > 0 and (
                args.max_polls is None or polls < args.max_polls
            ):
                time.sleep(remaining)
    except KeyboardInterrupt:
        print("Recording stopped by user.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
