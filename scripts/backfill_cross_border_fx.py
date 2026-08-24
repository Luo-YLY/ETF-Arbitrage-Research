"""Backfill post-close HKD/CNY into raw cross-border ETF recordings."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_arbitrage.data import SSEPCFParser, SZSEPCFParser
from etf_arbitrage.valuation import (
    backfill_cross_border_recording,
    load_hkd_cny_history,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use backward-only historical HKD/CNY to derive post-close model IOPV"
    )
    parser.add_argument("--pcf", required=True, type=Path)
    parser.add_argument("--fx-file", required=True, type=Path)
    parser.add_argument("--input", type=Path, help="raw JSONL recording")
    parser.add_argument("--output", type=Path, help="derived CSV output")
    parser.add_argument("--fx-timestamp-column", default="timestamp")
    parser.add_argument("--fx-rate-column")
    parser.add_argument("--fx-source", default="POST_CLOSE_IMPORT")
    parser.add_argument("--tolerance-seconds", type=float, default=60.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    pcf = (
        SSEPCFParser().parse(args.pcf)
        if args.pcf.suffix.lower() == ".json"
        else SZSEPCFParser().parse(args.pcf)
    )
    trade_date = pcf.trading_day.strftime("%Y%m%d")
    recording = args.input or (
        ROOT / "tmp" / "recordings" / trade_date / "{}.jsonl".format(pcf.etf_code)
    )
    output = args.output or (
        ROOT
        / "outputs"
        / "cross_border_backfill"
        / trade_date
        / pcf.etf_code
        / "observations.csv"
    )
    if not recording.exists():
        raise FileNotFoundError("原始行情记录不存在：{}".format(recording))
    fx = load_hkd_cny_history(
        args.fx_file,
        timestamp_column=args.fx_timestamp_column,
        rate_column=args.fx_rate_column,
    )
    result = backfill_cross_border_recording(
        recording,
        pcf,
        fx,
        tolerance_seconds=args.tolerance_seconds,
        fx_source=args.fx_source,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output, index=False, encoding="utf-8-sig")
    valid = int((result["valuation_status"] == "MODEL_IOPV_POST_CLOSE").sum())
    print(
        "FX backfill complete: rows={} valid={} pending={} output={}".format(
            len(result),
            valid,
            len(result) - valid,
            output,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
