"""Run selected SZSE ETF collectors during continuous-auction sessions."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, Optional


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_arbitrage.backtest import ResearchReplay
from etf_arbitrage.data import (
    JsonlSnapshotStore,
    PCFRepository,
    SZRedisDataFeed,
    SZRedisQuotationClient,
    SZRedisSettings,
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
    research: Optional[ResearchReplay]
    info: Any
    weights: Any
    expected_component_count: int
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
        auto_restart=bool(payload.get("auto_restart", True)),
        restart_delay=float(payload.get("restart_delay", 5.0)),
        max_restart_delay=float(payload.get("max_restart_delay", 60.0)),
        capture_only=bool(payload.get("capture_only", False)),
    )
    job.validate()
    return job


def build_trackers(job: MarketMonitorJob, client: SZRedisQuotationClient) -> list[Tracker]:
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
            etf_id=pcf.etf_id,
            component_ids={
                item.stock_code: item.instrument_id
                for item in pcf.components
            },
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
                research=(
                    None
                    if job.capture_only
                    else ResearchReplay(feed, calculator=PCFIOPVCalculator(pcf))
                ),
                info=info,
                weights=weights,
                expected_component_count=sum(
                    1
                    for item in pcf.components
                    if item.component_share > 0
                ),
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


def run_collection(
    args: argparse.Namespace,
    job: MarketMonitorJob,
    controller_state: Path,
    stop_path: Path,
    schedule: SZSEMarketSchedule,
    restart_count: int,
) -> int:
    trade_day = datetime.strptime(job.trade_date, "%Y%m%d").date()
    previous_state = read_job_state(controller_state)
    polls = _nonnegative_int(previous_state.get("polls"))
    previous_stored_counts = previous_state.get("stored_counts", {})
    if not isinstance(previous_stored_counts, dict):
        previous_stored_counts = {}
    errors: Dict[str, str] = {}

    update_state(
        controller_state,
        "starting",
        pid=os.getpid(),
        error=None,
        restart_count=restart_count,
        auto_restart=job.auto_restart,
        restart_delay=job.restart_delay,
        max_restart_delay=job.max_restart_delay,
        next_retry_at=None,
        retry_delay_seconds=None,
    )
    client = SZRedisQuotationClient(SZRedisSettings.from_env())
    client.ping()
    trackers = build_trackers(job, client)
    for tracker in trackers:
        tracker.stored_count = _nonnegative_int(
            previous_stored_counts.get(tracker.etf_code)
        )
    update_state(
        controller_state,
        "waiting",
        pcf_paths=job.pcf_paths,
        recording_paths={code.etf_code: str(code.store.path) for code in trackers},
        observation_paths={
            code.etf_code: str(code.observation_path) for code in trackers
        },
        restart_count=restart_count,
    )
    print("监控任务已就绪: {}".format(", ".join(job.etf_codes)), flush=True)

    attempt_polls = 0
    while True:
        now = datetime.now()
        if stop_path.exists():
            update_state(controller_state, "stopped", stopped_at=now.isoformat())
            print("收到停止请求，采集已结束。", flush=True)
            return 0
        if args.max_polls is not None and attempt_polls >= args.max_polls:
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
        attempt_polls += 1
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
                tracker.stored_count += 1
                if job.capture_only:
                    latest[tracker.etf_code] = {
                        "status": "stored",
                        "timestamp": snapshot.timestamp.isoformat(),
                        "price": snapshot.etf_quote.last_price,
                        "component_quotes": len(snapshot.stock_quotes),
                        "expected_components": tracker.expected_component_count,
                        "valuation_status": "PENDING_FX",
                    }
                    print(
                        "{} {} price={:.4f} components={}/{} raw_snapshot=stored "
                        "valuation=PENDING_FX".format(
                            snapshot.timestamp,
                            tracker.etf_code,
                            snapshot.etf_quote.last_price,
                            len(snapshot.stock_quotes),
                            tracker.expected_component_count,
                        ),
                        flush=True,
                    )
                else:
                    assert tracker.research is not None
                    row = tracker.research.process_snapshot(
                        snapshot, tracker.info, tracker.weights
                    )
                    append_observation(tracker.observation_path, row)
                    latest[tracker.etf_code] = {
                        "status": "stored",
                        "timestamp": snapshot.timestamp.isoformat(),
                        "price": row["etf_price"],
                        "iopv": row["iopv"],
                        "premium": row["premium"],
                        "valuation_quality": row["valuation_quality"],
                    }
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
                errors.pop(tracker.etf_code, None)
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
            restart_count=restart_count,
        )
        remaining = job.interval - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)


def retry_delay(job: MarketMonitorJob, restart_count: int) -> float:
    exponent = min(max(restart_count - 1, 0), 10)
    return min(job.restart_delay * (2**exponent), job.max_restart_delay)


def wait_for_restart(stop_path: Path, delay: float) -> bool:
    deadline = time.monotonic() + delay
    while time.monotonic() < deadline:
        if stop_path.exists():
            return False
        time.sleep(min(0.5, max(deadline - time.monotonic(), 0.0)))
    return not stop_path.exists()


def _nonnegative_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


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
    restart_count = _nonnegative_int(
        read_job_state(controller_state).get("restart_count")
    )

    while True:
        try:
            return run_collection(
                args,
                job,
                controller_state,
                stop_path,
                schedule,
                restart_count,
            )
        except Exception as exc:
            now = datetime.now()
            error = "{}: {}".format(type(exc).__name__, exc)
            if stop_path.exists():
                update_state(
                    controller_state,
                    "stopped",
                    stopped_at=now.isoformat(),
                    last_error=error,
                )
                return 0
            trade_day = datetime.strptime(job.trade_date, "%Y%m%d").date()
            if (
                not args.ignore_session
                and (
                    now.date() > trade_day
                    or (now.date() == trade_day and schedule.is_after_close(now))
                )
            ):
                update_state(
                    controller_state,
                    "completed",
                    completed_at=now.isoformat(),
                    last_error=error,
                )
                return 0
            if not job.auto_restart:
                update_state(
                    controller_state,
                    "error",
                    error=error,
                    last_error=error,
                )
                print("监控任务异常终止: {}".format(error), flush=True)
                return 1

            restart_count += 1
            delay = retry_delay(job, restart_count)
            next_retry_at = now + timedelta(seconds=delay)
            update_state(
                controller_state,
                "retry_wait",
                error=error,
                last_error=error,
                restart_count=restart_count,
                retry_delay_seconds=delay,
                next_retry_at=next_retry_at.isoformat(),
            )
            print(
                "监控任务异常: {}。将在 {:.0f} 秒后自动重启（第 {} 次）。".format(
                    error,
                    delay,
                    restart_count,
                ),
                flush=True,
            )
            if not wait_for_restart(stop_path, delay):
                update_state(
                    controller_state,
                    "stopped",
                    stopped_at=datetime.now().isoformat(),
                )
                print("等待重启期间收到停止请求，采集已结束。", flush=True)
                return 0


if __name__ == "__main__":
    raise SystemExit(main())
