"""Trading-session rules and a small background-process controller."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, Mapping, Optional, Union


@dataclass(frozen=True)
class SZSEMarketSchedule:
    morning_start: time = time(9, 30)
    morning_end: time = time(11, 30)
    afternoon_start: time = time(13, 0)
    afternoon_end: time = time(15, 0)

    def is_open(self, moment: datetime) -> bool:
        if moment.weekday() >= 5:
            return False
        current = moment.time()
        return (
            self.morning_start <= current <= self.morning_end
            or self.afternoon_start <= current <= self.afternoon_end
        )

    def is_after_close(self, moment: datetime) -> bool:
        return moment.weekday() < 5 and moment.time() > self.afternoon_end

    def phase(self, moment: datetime) -> str:
        if moment.weekday() >= 5:
            return "closed"
        current = moment.time()
        if current < self.morning_start:
            return "before_open"
        if self.morning_start <= current <= self.morning_end:
            return "morning_session"
        if current < self.afternoon_start:
            return "lunch_break"
        if current <= self.afternoon_end:
            return "afternoon_session"
        return "after_close"


@dataclass(frozen=True)
class MarketMonitorJob:
    trade_date: str
    etf_codes: tuple[str, ...]
    pcf_paths: Mapping[str, str]
    interval: float = 3.0
    redis_code_suffix: str = ".SZ"
    auto_restart: bool = True
    restart_delay: float = 5.0
    max_restart_delay: float = 60.0

    def validate(self) -> None:
        datetime.strptime(self.trade_date, "%Y%m%d")
        if not self.etf_codes:
            raise ValueError("至少选择一只ETF")
        if self.interval <= 0:
            raise ValueError("采集间隔必须为正数")
        if self.restart_delay <= 0:
            raise ValueError("自动重启等待时间必须为正数")
        if self.max_restart_delay < self.restart_delay:
            raise ValueError("最大重启等待时间不得小于首次等待时间")
        missing = [code for code in self.etf_codes if code not in self.pcf_paths]
        if missing:
            raise ValueError("缺少PCF: {}".format(", ".join(missing)))


class MarketMonitorController:
    """Start a worker detached from Streamlit and stop it through a flag file."""

    def __init__(self, project_root: Union[Path, str]) -> None:
        self.project_root = Path(project_root).resolve()
        self.runtime_dir = self.project_root / "tmp" / "runtime"

    def state_path(self, trade_date: str) -> Path:
        return self.runtime_dir / "market_monitor_{}.json".format(trade_date)

    def stop_path(self, trade_date: str) -> Path:
        return self.runtime_dir / "market_monitor_{}.stop".format(trade_date)

    def log_path(self, trade_date: str) -> Path:
        return self.runtime_dir / "market_monitor_{}.log".format(trade_date)

    def start(self, job: MarketMonitorJob) -> int:
        job.validate()
        state = read_job_state(self.state_path(job.trade_date))
        if state.get("status") in {
            "starting",
            "waiting",
            "running",
            "retry_wait",
        } and _pid_exists(state.get("pid")):
            raise RuntimeError("采集任务已经在运行")

        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.stop_path(job.trade_date).unlink(missing_ok=True)
        job_path = self.runtime_dir / "market_monitor_{}_job.json".format(job.trade_date)
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
            },
        )

        command = self.build_command(job_path)
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
            process = subprocess.Popen(command, **kwargs)
        finally:
            log_handle.close()
        state = read_job_state(self.state_path(job.trade_date))
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
            str(self.project_root / "scripts" / "run_market_monitor.py"),
            "--job",
            str(job_path),
        ]


def read_job_state(path: Union[Path, str]) -> Dict[str, Any]:
    source = Path(path)
    if not source.exists():
        return {}
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "invalid_state"}
    if payload.get("status") in {
        "starting",
        "waiting",
        "running",
        "retry_wait",
    } and not _pid_exists(payload.get("pid")):
        payload["status"] = "stopped"
    return payload


def write_job_state(path: Union[Path, str], payload: Mapping[str, Any]) -> None:
    _atomic_json_write(Path(path), payload)


def append_observation(path: Union[Path, str], observation: Mapping[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: _json_value(value) for key, value in observation.items()}
    with destination.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return sorted(value)
    return value


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def _pid_exists(pid: Optional[Any]) -> bool:
    if not pid:
        return False
    try:
        process_id = int(pid)
    except (TypeError, ValueError):
        return False
    if process_id <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_exists(process_id)
    try:
        os.kill(process_id, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_pid_exists(process_id: int) -> bool:
    """Check a Windows PID without sending it a signal.

    ``os.kill(pid, 0)`` is a harmless existence probe on POSIX, but Python's
    Windows implementation routes non-console signals through TerminateProcess.
    Using a query-only process handle avoids terminating the monitored worker.
    """

    import ctypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int

    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        process_id,
    )
    if not handle:
        # Access denied still proves that the PID exists.
        return ctypes.get_last_error() == 5
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)
