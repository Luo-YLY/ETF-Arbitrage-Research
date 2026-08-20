"""Run mainland multi-ETF five-level Redis collection and opportunity checks."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from math import isfinite
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from etf_arbitrage.arbitrage import ExecutableArbitrageDetector
from etf_arbitrage.data import PCFRepository, SZRedisSettings
from etf_arbitrage.domain import Exchange
from etf_arbitrage.executable_config import RedisConfig, RedisSnapshotFormat
from etf_arbitrage.market_data import NoNewSnapshotError, RedisMarketDataSource
from etf_arbitrage.operations import (
    ExecutableMonitorJob,
    SZSEMarketSchedule,
    append_observation,
    read_job_state,
    write_job_state,
)


@dataclass
class ExecutableTracker:
    etf_code: str
    source: RedisMarketDataSource
    detector: ExecutableArbitrageDetector
    result_path: Path
    active_components: int
    stored_count: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run mainland multi-ETF executable snapshot collection"
    )
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--ignore-session", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--max-polls", type=int, help=argparse.SUPPRESS)
    return parser.parse_args()


def load_job(path: Path) -> ExecutableMonitorJob:
    payload = json.loads(path.read_text(encoding="utf-8"))
    job = ExecutableMonitorJob(
        trade_date=str(payload["trade_date"]),
        etf_codes=tuple(str(code) for code in payload["etf_codes"]),
        pcf_paths={
            str(code): str(value) for code, value in payload["pcf_paths"].items()
        },
        interval=float(payload.get("interval", 3.0)),
        number_of_book_levels=int(payload.get("number_of_book_levels", 5)),
        max_workers=int(payload.get("max_workers", 4)),
        socket_timeout_seconds=float(payload.get("socket_timeout_seconds", 2.0)),
        auto_restart=bool(payload.get("auto_restart", True)),
        restart_delay=float(payload.get("restart_delay", 5.0)),
        max_restart_delay=float(payload.get("max_restart_delay", 60.0)),
    )
    job.validate()
    return job


def build_trackers(job: ExecutableMonitorJob) -> list[ExecutableTracker]:
    repository = PCFRepository(ROOT / "data" / "pcf")
    settings = SZRedisSettings.from_env()
    trackers = []
    for code in job.etf_codes:
        pcf_path = Path(job.pcf_paths[code]).resolve()
        pcf = repository.validate(pcf_path, code, job.trade_date)
        if pcf.etf_id.exchange not in {Exchange.SSE, Exchange.SZSE}:
            raise ValueError("{}不是境内上市ETF".format(code))
        non_mainland_components = [
            "{}({})".format(item.stock_code, item.instrument_id.exchange.value)
            for item in pcf.components
            if item.component_share > 0
            and item.instrument_id.exchange not in {Exchange.SSE, Exchange.SZSE}
        ]
        if non_mainland_components:
            raise ValueError(
                "{}的PCF含非沪深成分{}，本轮境内多ETF任务不接收跨境篮子".format(
                    code,
                    "、".join(non_mainland_components[:5]),
                )
            )
        recording_path = (
            ROOT
            / "tmp"
            / "executable_recordings"
            / job.trade_date
            / "{}.jsonl".format(code)
        )
        result_path = (
            ROOT
            / "tmp"
            / "executable_results"
            / job.trade_date
            / "{}.jsonl".format(code)
        )
        source = RedisMarketDataSource(
            RedisConfig(
                enabled=True,
                host=settings.host,
                port=settings.port,
                db=settings.db,
                password=settings.password,
                snapshot_format=RedisSnapshotFormat.DATE_HASH,
                trade_date_key=job.trade_date,
                number_of_book_levels=job.number_of_book_levels,
                poll_interval_ms=max(1, int(job.interval * 1_000)),
                recording_path=str(recording_path),
                socket_timeout_seconds=job.socket_timeout_seconds,
            ),
            code,
            pcf,
        )
        active_components = sum(
            1
            for item in pcf.components
            if item.component_share > 0
            and not item.substitute_flag.requires_cash_substitution
        )
        trackers.append(
            ExecutableTracker(
                etf_code=code,
                source=source,
                detector=ExecutableArbitrageDetector(pcf),
                result_path=result_path,
                active_components=active_components,
            )
        )
    return trackers


def collect_one(tracker: ExecutableTracker) -> dict[str, Any]:
    try:
        snapshot = tracker.source.step()
    except NoNewSnapshotError:
        return {
            "status": "unchanged",
            "stored_count": tracker.stored_count,
        }

    tracker.stored_count += 1
    two_sided = sum(
        1
        for book in snapshot.component_order_books.values()
        if book.has_two_sided_book
    )
    base = {
        "status": "stored",
        "timestamp": snapshot.snapshot_timestamp.isoformat(),
        "etf_bid_levels": len(snapshot.etf_order_book.bids),
        "etf_ask_levels": len(snapshot.etf_order_book.asks),
        "component_records": len(snapshot.component_order_books),
        "two_sided_components": two_sided,
        "active_components": tracker.active_components,
        "stored_count": tracker.stored_count,
    }
    try:
        evaluation = tracker.detector.evaluate(snapshot, include_capacity=False)
        result = {
            **base,
            "research_only": True,
            "internal_iopv": _finite_or_none(evaluation.internal_iopv),
            "quality_status": evaluation.quality.status.value,
            "quality_blockers": list(evaluation.quality.blockers),
            "quality_warnings": list(evaluation.quality.warnings),
            "creation_executable": evaluation.creation.executable,
            "creation_net_profit": _finite_or_none(evaluation.creation.net_profit),
            "creation_net_profit_bps": _finite_or_none(
                evaluation.creation.net_profit_bps
            ),
            "creation_rejections": list(evaluation.creation.rejection_reasons),
            "redemption_executable": evaluation.redemption.executable,
            "redemption_net_profit": _finite_or_none(
                evaluation.redemption.net_profit
            ),
            "redemption_net_profit_bps": _finite_or_none(
                evaluation.redemption.net_profit_bps
            ),
            "redemption_rejections": list(
                evaluation.redemption.rejection_reasons
            ),
        }
    except Exception as exc:
        result = {
            **base,
            "status": "stored_evaluation_error",
            "evaluation_error": "{}: {}".format(type(exc).__name__, exc),
        }
    append_observation(tracker.result_path, result)
    return result


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
    job: ExecutableMonitorJob,
    state_path: Path,
    stop_path: Path,
    schedule: SZSEMarketSchedule,
    restart_count: int,
) -> int:
    trade_day = datetime.strptime(job.trade_date, "%Y%m%d").date()
    previous = read_job_state(state_path)
    polls = _nonnegative_int(previous.get("polls"))
    previous_counts = previous.get("stored_counts", {})
    if not isinstance(previous_counts, dict):
        previous_counts = {}
    update_state(
        state_path,
        "starting",
        pid=os.getpid(),
        error=None,
        restart_count=restart_count,
        next_retry_at=None,
        retry_delay_seconds=None,
        research_only=True,
    )
    trackers = build_trackers(job)
    for tracker in trackers:
        tracker.stored_count = _nonnegative_int(previous_counts.get(tracker.etf_code))
    update_state(
        state_path,
        "waiting",
        pcf_paths=dict(job.pcf_paths),
        recording_paths={
            item.etf_code: item.source.config.recording_path for item in trackers
        },
        result_paths={item.etf_code: str(item.result_path) for item in trackers},
        restart_count=restart_count,
    )
    print("境内多ETF五档任务已就绪: {}".format(", ".join(job.etf_codes)), flush=True)

    attempts = 0
    executor = ThreadPoolExecutor(max_workers=min(job.max_workers, len(trackers)))
    try:
        while True:
            now = datetime.now()
            if stop_path.exists():
                update_state(state_path, "stopped", stopped_at=now.isoformat())
                print("收到停止请求，五档采集已结束。", flush=True)
                return 0
            if args.max_polls is not None and attempts >= args.max_polls:
                update_state(state_path, "completed", completed_at=now.isoformat())
                return 0
            if not args.ignore_session:
                if now.date() > trade_day or (
                    now.date() == trade_day and schedule.is_after_close(now)
                ):
                    update_state(state_path, "completed", completed_at=now.isoformat())
                    print("已收盘，五档采集任务自动结束。", flush=True)
                    return 0
                if now.date() != trade_day or not schedule.is_open(now):
                    update_state(
                        state_path,
                        "waiting",
                        market_phase=schedule.phase(now),
                    )
                    time.sleep(min(job.interval, 5.0))
                    continue

            started = time.monotonic()
            polls += 1
            attempts += 1
            latest: Dict[str, Any] = {}
            errors: Dict[str, str] = {}
            futures = {
                executor.submit(collect_one, tracker): tracker for tracker in trackers
            }
            for future in as_completed(futures):
                tracker = futures[future]
                try:
                    latest[tracker.etf_code] = future.result()
                except Exception as exc:
                    error = "{}: {}".format(type(exc).__name__, exc)
                    errors[tracker.etf_code] = error
                    latest[tracker.etf_code] = {
                        "status": "error",
                        "error": error,
                        "stored_count": tracker.stored_count,
                    }
                    print("{} 五档采集失败: {}".format(tracker.etf_code, error), flush=True)
            update_state(
                state_path,
                "running",
                market_phase=schedule.phase(now),
                polls=polls,
                latest=latest,
                errors=errors,
                stored_counts={
                    item.etf_code: item.stored_count for item in trackers
                },
                restart_count=restart_count,
            )
            remaining = job.interval - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        for tracker in trackers:
            tracker.source.disconnect()


def retry_delay(job: ExecutableMonitorJob, restart_count: int) -> float:
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


def _finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def main() -> int:
    args = parse_args()
    job = load_job(args.job)
    state_path = ROOT / "tmp" / "runtime" / "executable_monitor_{}.json".format(
        job.trade_date
    )
    stop_path = ROOT / "tmp" / "runtime" / "executable_monitor_{}.stop".format(
        job.trade_date
    )
    schedule = SZSEMarketSchedule()
    restart_count = _nonnegative_int(read_job_state(state_path).get("restart_count"))
    while True:
        try:
            return run_collection(
                args,
                job,
                state_path,
                stop_path,
                schedule,
                restart_count,
            )
        except Exception as exc:
            now = datetime.now()
            error = "{}: {}".format(type(exc).__name__, exc)
            if stop_path.exists():
                update_state(state_path, "stopped", stopped_at=now.isoformat())
                return 0
            trade_day = datetime.strptime(job.trade_date, "%Y%m%d").date()
            if not args.ignore_session and (
                now.date() > trade_day
                or (now.date() == trade_day and schedule.is_after_close(now))
            ):
                update_state(
                    state_path,
                    "completed",
                    completed_at=now.isoformat(),
                    last_error=error,
                )
                return 0
            if not job.auto_restart:
                update_state(state_path, "error", error=error, last_error=error)
                print("境内多ETF五档任务异常终止: {}".format(error), flush=True)
                return 1
            restart_count += 1
            delay = retry_delay(job, restart_count)
            update_state(
                state_path,
                "retry_wait",
                error=error,
                last_error=error,
                restart_count=restart_count,
                retry_delay_seconds=delay,
                next_retry_at=(now + timedelta(seconds=delay)).isoformat(),
            )
            print(
                "境内多ETF五档任务异常: {}。将在 {:.0f} 秒后自动重启。".format(
                    error, delay
                ),
                flush=True,
            )
            if not wait_for_restart(stop_path, delay):
                update_state(
                    state_path,
                    "stopped",
                    stopped_at=datetime.now().isoformat(),
                )
                return 0


if __name__ == "__main__":
    raise SystemExit(main())
