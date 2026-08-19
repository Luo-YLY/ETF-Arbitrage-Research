"""Read-only preflight for raw Redis five-level executable snapshots."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_arbitrage.arbitrage import ExecutableArbitrageDetector
from etf_arbitrage.data import SSEPCFParser, SZRedisSettings, SZSEPCFParser
from etf_arbitrage.executable_config import RedisConfig, RedisSnapshotFormat
from etf_arbitrage.market_data import (
    RedisMarketDataSource,
    cross_border_hk_components,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate one raw Redis five-level ETF/PCF snapshot"
    )
    parser.add_argument("--pcf", type=Path, required=True)
    parser.add_argument("--etf-code")
    parser.add_argument("--trade-date", help="Redis Hash key in YYYYMMDD format")
    parser.add_argument(
        "--hkd-cny-code",
        default=os.getenv("SZ_REDIS_HKD_CNY_CODE", ""),
    )
    parser.add_argument("--levels", type=int, default=5)
    return parser.parse_args()


def _runtime_pcf(pcf):
    hk_components = tuple(cross_border_hk_components(pcf))
    if not hk_components:
        return pcf
    return replace(
        pcf,
        components=hk_components,
        total_record_num=len(hk_components),
    )


def main() -> int:
    args = parse_args()
    if not 1 <= args.levels <= 10:
        raise ValueError("--levels must be between 1 and 10")
    parser = SSEPCFParser() if args.pcf.suffix.lower() == ".json" else SZSEPCFParser()
    try:
        pcf = parser.parse(args.pcf)
        if args.etf_code and args.etf_code != pcf.etf_code:
            raise ValueError("--etf-code does not match PCF")
        runtime_pcf = _runtime_pcf(pcf)
        settings = SZRedisSettings.from_env()
        source = RedisMarketDataSource(
            RedisConfig(
                enabled=True,
                host=settings.host,
                port=settings.port,
                db=settings.db,
                password=settings.password,
                snapshot_format=RedisSnapshotFormat.DATE_HASH,
                trade_date_key=(
                    args.trade_date or runtime_pcf.trading_day.strftime("%Y%m%d")
                ),
                hkd_cny_code=args.hkd_cny_code.strip(),
                number_of_book_levels=args.levels,
                socket_timeout_seconds=settings.socket_timeout,
            ),
            runtime_pcf.etf_code,
            runtime_pcf,
        )
        snapshot = source.step()
        evaluation = ExecutableArbitrageDetector(runtime_pcf).evaluate(snapshot)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1
    finally:
        if "source" in locals():
            source.disconnect()

    active = [
        item
        for item in runtime_pcf.components
        if item.component_share > 0
        and not item.substitute_flag.requires_cash_substitution
    ]
    two_sided = sum(
        1
        for item in active
        if snapshot.component_order_books.get(item.stock_code) is not None
        and snapshot.component_order_books[item.stock_code].has_two_sided_book
    )
    quality = evaluation.quality
    etf_two_sided = snapshot.etf_order_book.has_two_sided_book
    payload = {
        "ok": (
            not quality.blockers
            and etf_two_sided
            and two_sided == len(active)
        ),
        "research_only": True,
        "etf_code": runtime_pcf.etf_code,
        "trade_date": runtime_pcf.trading_day.isoformat(),
        "snapshot_timestamp": snapshot.snapshot_timestamp.isoformat(),
        "event_watermark": (
            snapshot.event_watermark.isoformat() if snapshot.event_watermark else None
        ),
        "etf_bid_levels": len(snapshot.etf_order_book.bids),
        "etf_ask_levels": len(snapshot.etf_order_book.asks),
        "etf_two_sided": etf_two_sided,
        "active_components": len(active),
        "component_records": len(snapshot.component_order_books),
        "two_sided_components": two_sided,
        "hkd_cny_present": snapshot.hkd_cny_quote is not None,
        "quality_status": quality.status.value,
        "quality_blockers": list(quality.blockers),
        "quality_warnings": list(quality.warnings),
        "coverage": quality.coverage,
        "maximum_quote_age_ms": max(
            quality.etf_quote_age_ms,
            quality.max_component_quote_age_ms,
        ),
        "cross_section_skew_ms": quality.max_cross_section_skew_ms,
        "assembly_blockers": list(snapshot.assembly_blockers),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
