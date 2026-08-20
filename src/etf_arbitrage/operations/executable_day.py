"""Background controller for mainland multi-ETF five-level collection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, Mapping, Union

from .market_day import _atomic_json_write, _pid_exists, read_job_state


@dataclass(frozen=True)
class ExecutableMonitorJob:
    """Configuration for one trading day's mainland executable snapshots."""

    trade_date: str
    etf_codes: tuple[str, ...]
    pcf_paths: Mapping[str, str]
    interval: float = 3.0
    number_of_book_levels: int = 5
    max_workers: int = 4
    socket_timeout_seconds: float = 2.0
    auto_restart: bool = True
    restart_delay: float = 5.0
    max_restart_delay: float = 60.0

    def validate(self) -> None:
        datetime.strptime(self.trade_date, "%Y%m%d")
        if not self.etf_codes:
            raise ValueError("至少选择一只ETF")
        if len(set(self.etf_codes)) != len(self.etf_codes):
            raise ValueError("ETF列表不能包含重复代码")
        if self.interval <= 0:
            raise ValueError("采集间隔必须为正数")
        if not 1 <= self.number_of_book_levels <= 10:
            raise ValueError("盘口档位必须在1到10之间")
        if not 1 <= self.max_workers <= 16:
            raise ValueError("并行工作线程必须在1到16之间")
        if self.socket_timeout_seconds <= 0:
            raise ValueError("Redis连接超时必须为正数")
        if self.restart_delay <= 0:
            raise ValueError("自动重启等待时间必须为正数")
        if self.max_restart_delay < self.restart_delay:
            raise ValueError("最大重启等待时间不得小于首次等待时间")
        missing = [code for code in self.etf_codes if code not in self.pcf_paths]
        if missing:
            raise ValueError("缺少PCF: {}".format(", ".join(missing)))


class ExecutableMonitorController:
    """Start and stop the detached mainland five-level collection worker."""

    def __init__(self, project_root: Union[Path, str]) -> None:
        self.project_root = Path(project_root).resolve()
        self.runtime_dir = self.project_root / "tmp" / "runtime"

    def state_path(self, trade_date: str) -> Path:
        return self.runtime_dir / "executable_monitor_{}.json".format(trade_date)

    def stop_path(self, trade_date: str) -> Path:
        return self.runtime_dir / "executable_monitor_{}.stop".format(trade_date)

    def log_path(self, trade_date: str) -> Path:
        return self.runtime_dir / "executable_monitor_{}.log".format(trade_date)

    def result_dir(self, trade_date: str) -> Path:
        return self.project_root / "tmp" / "executable_results" / trade_date

    def recording_dir(self, trade_date: str) -> Path:
        return self.project_root / "tmp" / "executable_recordings" / trade_date

    def start(self, job: ExecutableMonitorJob) -> int:
        job.validate()
        state = read_job_state(self.state_path(job.trade_date))
        if state.get("status") in {
            "starting",
            "waiting",
            "running",
            "retry_wait",
        } and _pid_exists(state.get("pid")):
            raise RuntimeError("境内多ETF五档采集任务已经在运行")

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.stop_path(job.trade_date).unlink(missing_ok=True)
        job_path = self.runtime_dir / "executable_monitor_{}_job.json".format(
            job.trade_date
        )
        _atomic_json_write(job_path, asdict(job))
        _atomic_json_write(
            self.state_path(job.trade_date),
            {
                "status": "starting",
                "trade_date": job.trade_date,
                "etf_codes": list(job.etf_codes),
                "started_at": datetime.now().isoformat(),
                "auto_restart": job.auto_restart,
                "restart_delay": job.restart_delay,
                "max_restart_delay": job.max_restart_delay,
                "research_only": True,
            },
        )

        log_handle = self.log_path(job.trade_date).open("a", encoding="utf-8")
        kwargs: Dict[str, Any] = {
            "cwd": str(self.project_root),
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "env": dict(
                os.environ,
                PYTHONUNBUFFERED="1",
                PYTHONIOENCODING="utf-8",
                PYTHONUTF8="1",
            ),
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        else:
            kwargs["start_new_session"] = True
        try:
            process = subprocess.Popen(self.build_command(job_path), **kwargs)
        finally:
            log_handle.close()
        try:
            state = json.loads(
                self.state_path(job.trade_date).read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            state = {"status": "starting", "trade_date": job.trade_date}
        state["pid"] = process.pid
        _atomic_json_write(self.state_path(job.trade_date), state)
        return process.pid

    def request_stop(self, trade_date: str) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.stop_path(trade_date).write_text(
            datetime.now().isoformat(), encoding="utf-8"
        )

    def build_command(self, job_path: Path) -> list[str]:
        return [
            sys.executable,
            str(self.project_root / "scripts" / "run_executable_monitor.py"),
            "--job",
            str(job_path),
        ]
