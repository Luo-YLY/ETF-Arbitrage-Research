"""Run selected SZSE ETF collectors during continuous-auction sessions."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_arbitrage.backtest import ResearchReplay
from etf_arbitrage.data import (
    JsonlSnapshotStore,
    PCFRepository,
    SZRedisDataFeed,
    SZRedisQuotationClient,
    SZRedisSettings,
    SZSEPCFParser,
)
from etf_arbitrage.operations import (
    MarketMonitorJob,
    SZSEMarketSchedule,
    append_observation,
    read_job_state,
    write_job_state,
)
from etf_arbitrage.valuation import PCFIOPVCalculator


@dataclass
class Tracker:
    etf_code: str
    pcf_path: Path
    feed: SZRedisDataFeed
    store: JsonlSnapshotStore
    observation_path: Path
    research: ResearchReplay
    info: Any
    weights: Any
    stored_count: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the SZSE market-session monitor")
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--ignore-session", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--max-polls", type=int, help=argparse.SUPPRESS)
    return parser.parse_args()


def load_job(path: Path) -> MarketMonitorJob:
    payload = json.loads(path.read_text(encoding="utf-8"))
    job = MarketMonitorJob(
        trade_date=str(payload["trade_date"]),
        etf_codes=tuple(payload["etf_codes"]),
        pcf_paths={str(code): str(value) for code, value in payload["pcf_paths"].items()},
        interval=float(payload.get("interval", 3.0)),
        redis_code_suffix=str(payload.get("redis_code_suffix", ".SZ")),
    )
    job.validate()
    return job


def build_trackers(job: MarketMonitorJob, client: SZRedisQuotationClient) -> list[Tracker]:
    parser = SZSEPCFParser()
    repository = PCFRepository(ROOT / "data" / "pcf")
    trackers = []
    for code in job.etf_codes:
        pcf_path = Path(job.pcf_paths[code]).resolve()
        pcf = repository.validate(pcf_path, code, job.trade_date)
        info = pcf.to_etf_info()
        weights = pcf.component_weights()
        feed = SZRedisDataFeed(
            client=client,
            etf_info=info,
            weights=weights,
            trade_date=job.trade_date,
            redis_code_suffix=job.redis_code_suffix,
            require_bid_ask=False,
        )
        trackers.append(
            Tracker(
                etf_code=code,
                pcf_path=pcf_path,
                feed=feed,
                store=JsonlSnapshotStore(
                    ROOT / "tmp" / "recordings" / job.trade_date / "{}.jsonl".format(code)
                ),
                observation_path=(
                    ROOT / "tmp" / "observations" / job.trade_date / "{}.jsonl".format(code)
                ),
                research=ResearchReplay(feed, calculator=PCFIOPVCalculator(pcf)),
                info=info,
                weights=weights,
            )
        )
    return trackers


def update_state(path: Path, status: str, **updates: Any) -> Dict[str, Any]:
    state = read_job_state(path)
    state.update(updates)
    state["status"] = status
    state["updated_at"] = datetime.now().isoformat()
    state.setdefault("pid", os.getpid())
    write_job_state(path, state)
    return state


def main() -> int:
    args = parse_args()
    job = load_job(args.job)
    controller_state = ROOT / "tmp" / "runtime" / "market_monitor_{}.json".format(
        job.trade_date
    )
    stop_path = ROOT / "tmp" / "runtime" / "market_monitor_{}.stop".format(
        job.trade_date
    )
    schedule = SZSEMarketSchedule()
    trade_day = datetime.strptime(job.trade_date, "%Y%m%d").date()
    polls = 0
    errors: Dict[str, str] = {}

    try:
        update_state(controller_state, "starting", pid=os.getpid(), error=None)
        client = SZRedisQuotationClient(SZRedisSettings.from_env())
        client.ping()
        trackers = build_trackers(job, client)
        update_state(
            controller_state,
            "waiting",
            pcf_paths=job.pcf_paths,
            recording_paths={code.etf_code: str(code.store.path) for code in trackers},
            observation_paths={
                code.etf_code: str(code.observation_path) for code in trackers
            },
        )
        print("监控任务已就绪: {}".format(", ".join(job.etf_codes)), flush=True)

        while True:
            now = datetime.now()
            if stop_path.exists():
                update_state(controller_state, "stopped", stopped_at=now.isoformat())
                print("收到停止请求，采集已结束。", flush=True)
                return 0
            if args.max_polls is not None and polls >= args.max_polls:
                update_state(controller_state, "completed", completed_at=now.isoformat())
                return 0
            if not args.ignore_session:
                if now.date() > trade_day or (
                    now.date() == trade_day and schedule.is_after_close(now)
                ):
                    update_state(controller_state, "completed", completed_at=now.isoformat())
                    print("已收盘，采集任务自动结束。", flush=True)
                    return 0
                if now.date() != trade_day or not schedule.is_open(now):
                    update_state(
                        controller_state,
                        "waiting",
                        market_phase=schedule.phase(now),
                    )
                    time.sleep(min(job.interval, 5.0))
                    continue

            started = time.monotonic()
            polls += 1
            latest: Dict[str, Any] = {}
            for tracker in trackers:
                try:
                    snapshots = list(tracker.feed.snapshots(tracker.etf_code))
                    if not snapshots:
                        latest[tracker.etf_code] = {"status": "no_redis_record"}
                        continue
                    snapshot = snapshots[0]
                    stored = tracker.store.append(snapshot)
                    if not stored:
                        latest[tracker.etf_code] = {
                            "status": "unchanged",
                            "timestamp": snapshot.timestamp.isoformat(),
                        }
                        continue
                    row = tracker.research.process_snapshot(
                        snapshot, tracker.info, tracker.weights
                    )
                    append_observation(tracker.observation_path, row)
                    tracker.stored_count += 1
                    latest[tracker.etf_code] = {
                        "status": "stored",
                        "timestamp": snapshot.timestamp.isoformat(),
                        "price": row["etf_price"],
                        "iopv": row["iopv"],
                        "premium": row["premium"],
                        "valuation_quality": row["valuation_quality"],
                    }
                    errors.pop(tracker.etf_code, None)
                    print(
                        "{} {} price={:.4f} iopv={:.4f} premium={:.4%}".format(
                            snapshot.timestamp,
                            tracker.etf_code,
                            row["etf_price"],
                            row["iopv"],
                            row["premium"],
                        ),
                        flush=True,
                    )
                except Exception as exc:
                    errors[tracker.etf_code] = "{}: {}".format(type(exc).__name__, exc)
                    latest[tracker.etf_code] = {
                        "status": "error",
                        "error": errors[tracker.etf_code],
                    }
                    print("{} 采集失败: {}".format(tracker.etf_code, exc), flush=True)

            update_state(
                controller_state,
                "running",
                market_phase=schedule.phase(now),
                polls=polls,
                latest=latest,
                stored_counts={item.etf_code: item.stored_count for item in trackers},
                errors=errors,
            )
            remaining = job.interval - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
    except Exception as exc:
        update_state(
            controller_state,
            "error",
            error="{}: {}".format(type(exc).__name__, exc),
        )
        print("监控任务异常终止: {}".format(exc), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
