"""Probe one Shenzhen Redis quotation record without modifying Redis."""

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_arbitrage.data import SZRedisQuotationClient, SZRedisSettings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read one quotation record from Redis")
    parser.add_argument("--code", default="159915", help="security code stored in Redis")
    parser.add_argument("--date", help="Redis hash key in YYYYMMDD format")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        settings = SZRedisSettings.from_env()
        client = SZRedisQuotationClient(settings)
        client.ping()
        record = client.get_security_record(args.code, args.date)
    except Exception as exc:
        print("Redis quotation probe failed: {}".format(exc), file=sys.stderr)
        return 1

    if record is None:
        print("No quotation record found for {}".format(args.code))
        return 2
    print("Available fields: {}".format(", ".join(sorted(record))))
    print(json.dumps(record, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
