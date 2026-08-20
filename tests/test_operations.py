from datetime import datetime
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

import pandas as pd
import pytest

from etf_arbitrage.data import PCFRepository, PCFValidationError
from etf_arbitrage.operations import (
    ExecutableMonitorController,
    ExecutableMonitorJob,
    MarketMonitorController,
    MarketMonitorJob,
    SZSEMarketSchedule,
    read_job_state,
    write_job_state,
)
from etf_arbitrage.operations.market_day import _pid_exists
from etf_arbitrage.dashboard.app import _resample_last
from scripts import run_executable_monitor, run_market_monitor


PCF_SAMPLE = Path("data/pcf/20260722/pcf_159915_20260722.xml")
SZSE_DOWNLOAD_PAGE = (
    "https://www.szse.cn/modules/report/views/eft_download_new.html?"
    "path=%2Ffiles%2Ftext%2FETFDown%2F&"
    "filename=pcf_159915_20260722%3B159915ETF20260722&"
    "opencode=ETF15991520260722.txt"
)


def test_market_schedule_covers_sessions_and_lunch_break() -> None:
    schedule = SZSEMarketSchedule()

    assert not schedule.is_open(datetime(2026, 7, 22, 9, 29, 59))
    assert schedule.is_open(datetime(2026, 7, 22, 9, 30))
    assert schedule.is_open(datetime(2026, 7, 22, 11, 30))
    assert not schedule.is_open(datetime(2026, 7, 22, 12, 0))
    assert schedule.is_open(datetime(2026, 7, 22, 13, 0))
    assert schedule.is_open(datetime(2026, 7, 22, 15, 0))
    assert schedule.is_after_close(datetime(2026, 7, 22, 15, 0, 1))


def test_dashboard_chart_resampling_keeps_last_value_in_each_bucket() -> None:
    frame = pd.DataFrame(
        {
            "timestamp": [
                datetime(2026, 7, 22, 9, 30, 3),
                datetime(2026, 7, 22, 9, 30, 57),
                datetime(2026, 7, 22, 9, 31, 3),
            ],
            "premium": [0.001, 0.002, 0.003],
        }
    )

    result = _resample_last(frame, "1min", "premium")

    assert result["premium"].tolist() == [0.002, 0.003]


def test_pcf_repository_saves_and_validates_xml_and_zip() -> None:
    root = Path("tmp") / "tests" / uuid4().hex
    try:
        repository = PCFRepository(root)
        content = PCF_SAMPLE.read_bytes()
        xml_path = repository.save(content, "159915", "20260722")

        assert xml_path.exists()
        assert repository.find("159915", "20260722") == xml_path

        archive = BytesIO()
        with ZipFile(archive, "w") as handle:
            handle.writestr("files/pcf_159915.xml", content)
        zip_path = repository.save(archive.getvalue(), "159915", "20260722")

        assert zip_path == xml_path
        assert repository.validate(zip_path, "159915", "20260722").record_num == 100
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_pcf_repository_rejects_wrong_etf_or_date() -> None:
    repository = PCFRepository(Path("tmp") / "tests" / uuid4().hex)

    with pytest.raises(PCFValidationError, match="证券代码"):
        repository.save(PCF_SAMPLE.read_bytes(), "159901", "20260722")
    with pytest.raises(PCFValidationError, match="交易日"):
        repository.save(PCF_SAMPLE.read_bytes(), "159915", "20260723")


def test_szse_download_page_resolves_to_direct_pcf_candidates() -> None:
    candidates = PCFRepository.download_candidates(
        SZSE_DOWNLOAD_PAGE, "159915", "20260722"
    )

    assert candidates[0] == (
        "https://reportdocs.static.szse.cn/files/text/ETFDown/"
        "pcf_159915_20260722.xml"
    )
    assert candidates[1] == (
        "https://reportdocs.static.sse.org.cn/files/text/ETFDown/"
        "pcf_159915_20260722.xml"
    )
    assert candidates[2].endswith("pcf_159915_20260722.txt")
    assert candidates[-3].endswith("159915ETF20260722.xml")
    assert candidates[-1].endswith("159915ETF20260722.txt")


def test_direct_report_url_includes_official_backup_host() -> None:
    candidates = PCFRepository.download_candidates(
        "https://reportdocs.static.szse.cn/files/text/ETFDown/"
        "pcf_159915_20260723.xml",
        "159915",
        "20260723",
    )

    assert candidates == [
        "https://reportdocs.static.szse.cn/files/text/ETFDown/"
        "pcf_159915_20260723.xml",
        "https://reportdocs.static.sse.org.cn/files/text/ETFDown/"
        "pcf_159915_20260723.xml",
    ]


def test_repository_downloads_from_szse_landing_page(monkeypatch) -> None:
    root = Path("tmp") / "tests" / uuid4().hex
    requested_urls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self):
            return PCF_SAMPLE.read_bytes()

    def fake_urlopen(request, timeout):
        requested_urls.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr("etf_arbitrage.data.pcf_repository.urlopen", fake_urlopen)
    try:
        path = PCFRepository(root).download(
            SZSE_DOWNLOAD_PAGE, "159915", "20260722"
        )

        assert path.exists()
        assert requested_urls == [
            "https://reportdocs.static.szse.cn/files/text/ETFDown/"
            "pcf_159915_20260722.xml"
        ]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_repository_falls_back_to_official_report_host(monkeypatch) -> None:
    root = Path("tmp") / "tests" / uuid4().hex
    requested_urls = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self):
            return PCF_SAMPLE.read_bytes()

    def fake_urlopen(request, timeout):
        requested_urls.append(request.full_url)
        if request.full_url.startswith("https://reportdocs.static.szse.cn"):
            raise OSError("primary report host unavailable")
        return FakeResponse()

    monkeypatch.setattr("etf_arbitrage.data.pcf_repository.urlopen", fake_urlopen)
    try:
        path = PCFRepository(root).download(
            SZSE_DOWNLOAD_PAGE, "159915", "20260722"
        )

        assert path.exists()
        assert requested_urls == [
            "https://reportdocs.static.szse.cn/files/text/ETFDown/"
            "pcf_159915_20260722.xml",
            "https://reportdocs.static.sse.org.cn/files/text/ETFDown/"
            "pcf_159915_20260722.xml",
        ]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_repository_reports_html_instead_of_xml() -> None:
    repository = PCFRepository(Path("tmp") / "tests" / uuid4().hex)

    with pytest.raises(PCFValidationError, match="返回的是网页"):
        repository.save(b"<!DOCTYPE html><html></html>", "159915", "20260722")


def test_monitor_job_validation_and_command() -> None:
    controller = MarketMonitorController(Path.cwd())
    job = MarketMonitorJob(
        trade_date="20260722",
        etf_codes=("159915",),
        pcf_paths={"159915": "sample.xml"},
    )

    job.validate()
    command = controller.build_command(Path("job.json"))

    assert job.redis_code_suffix == ".SZ"
    assert job.auto_restart
    assert job.restart_delay == 5.0
    assert job.max_restart_delay == 60.0
    assert not job.capture_only
    assert command[0]
    assert command[1].endswith("run_market_monitor.py")
    assert command[-2:] == ["--job", "job.json"]


def test_monitor_job_requires_every_selected_pcf() -> None:
    job = MarketMonitorJob(
        trade_date="20260722",
        etf_codes=("159915", "159901"),
        pcf_paths={"159915": "sample.xml"},
    )

    with pytest.raises(ValueError, match="159901"):
        job.validate()


def test_executable_monitor_job_validates_parallel_mainland_collection() -> None:
    controller = ExecutableMonitorController(Path.cwd())
    job = ExecutableMonitorJob(
        trade_date="20260722",
        etf_codes=("159915", "510300"),
        pcf_paths={"159915": "one.xml", "510300": "two.json"},
        interval=3.0,
        max_workers=2,
    )

    job.validate()
    command = controller.build_command(Path("executable_job.json"))

    assert job.number_of_book_levels == 5
    assert job.auto_restart
    assert command[0]
    assert command[1].endswith("run_executable_monitor.py")
    assert command[-2:] == ["--job", "executable_job.json"]


def test_executable_monitor_job_requires_every_selected_pcf() -> None:
    job = ExecutableMonitorJob(
        trade_date="20260722",
        etf_codes=("159915", "510300"),
        pcf_paths={"159915": "one.xml"},
    )

    with pytest.raises(ValueError, match="510300"):
        job.validate()


def test_executable_monitor_controller_starts_detached_utf8_worker(monkeypatch) -> None:
    root = Path("tmp") / "tests" / uuid4().hex
    captured = {}

    class FakeProcess:
        pid = 24680

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(
        "etf_arbitrage.operations.executable_day.subprocess.Popen",
        fake_popen,
    )
    try:
        controller = ExecutableMonitorController(root)
        job = ExecutableMonitorJob(
            trade_date="20260722",
            etf_codes=("159915", "510300"),
            pcf_paths={"159915": "one.xml", "510300": "two.json"},
        )

        pid = controller.start(job)

        assert pid == 24680
        assert captured["command"][1].endswith("run_executable_monitor.py")
        environment = captured["kwargs"]["env"]
        assert environment["PYTHONUNBUFFERED"] == "1"
        assert environment["PYTHONIOENCODING"] == "utf-8"
        assert environment["PYTHONUTF8"] == "1"
        state = json.loads(
            controller.state_path(job.trade_date).read_text(encoding="utf-8")
        )
        assert state["status"] == "starting"
        assert state["pid"] == 24680
        assert state["etf_codes"] == ["159915", "510300"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_executable_monitor_builds_independent_recording_for_each_etf(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SZ_REDIS_HOST", "example.invalid")
    job = ExecutableMonitorJob(
        trade_date="20260722",
        etf_codes=("159915",),
        pcf_paths={"159915": str(PCF_SAMPLE.resolve())},
    )

    trackers = run_executable_monitor.build_trackers(job)

    assert len(trackers) == 1
    tracker = trackers[0]
    assert tracker.etf_code == "159915"
    assert tracker.active_components == 98
    assert tracker.source.config.recording_path.endswith(
        "tmp\\executable_recordings\\20260722\\159915.jsonl"
    )
    assert str(tracker.result_path).endswith(
        "tmp\\executable_results\\20260722\\159915.jsonl"
    )


def test_executable_monitor_rejects_non_mainland_pcf_components(monkeypatch) -> None:
    monkeypatch.setenv("SZ_REDIS_HOST", "example.invalid")

    class FakeRepository:
        def __init__(self, _root):
            pass

        def validate(self, _path, _code, _trade_date):
            return SimpleNamespace(
                etf_id=SimpleNamespace(exchange=run_executable_monitor.Exchange.SZSE),
                components=(
                    SimpleNamespace(
                        stock_code="00700",
                        component_share=100.0,
                        instrument_id=SimpleNamespace(
                            exchange=run_executable_monitor.Exchange.HKEX
                        ),
                    ),
                ),
            )

    monkeypatch.setattr(run_executable_monitor, "PCFRepository", FakeRepository)
    job = ExecutableMonitorJob(
        trade_date="20260722",
        etf_codes=("159920",),
        pcf_paths={"159920": "cross_border.xml"},
    )

    with pytest.raises(ValueError, match="非沪深成分"):
        run_executable_monitor.build_trackers(job)


def test_executable_monitor_collects_snapshot_and_writes_quality_result() -> None:
    result_path = Path("tmp") / "tests" / "{}.jsonl".format(uuid4().hex)
    result_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = SimpleNamespace(
        snapshot_timestamp=datetime(2026, 7, 22, 10, 0),
        etf_order_book=SimpleNamespace(bids=(1, 2), asks=(1, 2)),
        component_order_books={
            "000001": SimpleNamespace(has_two_sided_book=True),
            "000002": SimpleNamespace(has_two_sided_book=False),
        },
    )
    evaluation = SimpleNamespace(
        internal_iopv=1.234,
        quality=SimpleNamespace(
            status=SimpleNamespace(value="BLOCKED"),
            blockers=("MISSING_COMPONENT_TWO_SIDED_BOOK",),
            warnings=(),
        ),
        creation=SimpleNamespace(
            executable=False,
            net_profit=-1.0,
            net_profit_bps=-0.5,
            rejection_reasons=("MISSING_COMPONENT_TWO_SIDED_BOOK",),
        ),
        redemption=SimpleNamespace(
            executable=False,
            net_profit=-2.0,
            net_profit_bps=-1.0,
            rejection_reasons=("MISSING_COMPONENT_TWO_SIDED_BOOK",),
        ),
    )

    class FakeSource:
        def step(self):
            return snapshot

    class FakeDetector:
        def evaluate(self, _snapshot, include_capacity):
            assert include_capacity is False
            return evaluation

    tracker = run_executable_monitor.ExecutableTracker(
        etf_code="159915",
        source=FakeSource(),
        detector=FakeDetector(),
        result_path=result_path,
        active_components=2,
    )
    try:
        latest = run_executable_monitor.collect_one(tracker)
        saved = json.loads(result_path.read_text(encoding="utf-8"))

        assert latest["status"] == "stored"
        assert latest["two_sided_components"] == 1
        assert latest["stored_count"] == 1
        assert saved["research_only"] is True
        assert saved["quality_status"] == "BLOCKED"
        assert saved["creation_executable"] is False
    finally:
        result_path.unlink(missing_ok=True)


def test_capture_only_monitor_does_not_build_live_iopv_replay() -> None:
    job = MarketMonitorJob(
        trade_date="20260722",
        etf_codes=("159915",),
        pcf_paths={"159915": str(PCF_SAMPLE)},
        capture_only=True,
    )

    trackers = run_market_monitor.build_trackers(job, object())

    assert len(trackers) == 1
    assert trackers[0].research is None
    assert trackers[0].expected_component_count == 98


def test_monitor_job_validates_restart_delays() -> None:
    with pytest.raises(ValueError, match="自动重启等待时间"):
        MarketMonitorJob(
            trade_date="20260722",
            etf_codes=("159915",),
            pcf_paths={"159915": "sample.xml"},
            restart_delay=0,
        ).validate()

    with pytest.raises(ValueError, match="最大重启等待时间"):
        MarketMonitorJob(
            trade_date="20260722",
            etf_codes=("159915",),
            pcf_paths={"159915": "sample.xml"},
            restart_delay=10,
            max_restart_delay=5,
        ).validate()


def test_monitor_retry_delay_uses_capped_backoff() -> None:
    job = MarketMonitorJob(
        trade_date="20260722",
        etf_codes=("159915",),
        pcf_paths={"159915": "sample.xml"},
        restart_delay=5,
        max_restart_delay=20,
    )

    assert [run_market_monitor.retry_delay(job, attempt) for attempt in range(1, 6)] == [
        5,
        10,
        20,
        20,
        20,
    ]


def test_monitor_worker_restarts_after_fatal_error(monkeypatch) -> None:
    root = Path("tmp") / "tests" / uuid4().hex
    job = MarketMonitorJob(
        trade_date="20260722",
        etf_codes=("159915",),
        pcf_paths={"159915": "sample.xml"},
        restart_delay=1,
    )
    args = SimpleNamespace(
        job=root / "job.json",
        ignore_session=True,
        max_polls=None,
    )
    attempts = []

    def fake_run_collection(
        args,
        job,
        controller_state,
        stop_path,
        schedule,
        restart_count,
    ):
        attempts.append(restart_count)
        if len(attempts) == 1:
            raise ConnectionError("temporary redis failure")
        run_market_monitor.update_state(
            controller_state,
            "completed",
            restart_count=restart_count,
        )
        return 0

    monkeypatch.setattr(run_market_monitor, "ROOT", root)
    monkeypatch.setattr(run_market_monitor, "parse_args", lambda: args)
    monkeypatch.setattr(run_market_monitor, "load_job", lambda _path: job)
    monkeypatch.setattr(run_market_monitor, "run_collection", fake_run_collection)
    monkeypatch.setattr(
        run_market_monitor,
        "wait_for_restart",
        lambda _stop_path, _delay: True,
    )
    try:
        assert run_market_monitor.main() == 0
        state = read_job_state(
            root / "tmp" / "runtime" / "market_monitor_20260722.json"
        )
        assert attempts == [0, 1]
        assert state["status"] == "completed"
        assert state["restart_count"] == 1
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_retry_wait_is_treated_as_an_active_job() -> None:
    root = Path("tmp") / "tests" / uuid4().hex
    controller = MarketMonitorController(root)
    job = MarketMonitorJob(
        trade_date="20260722",
        etf_codes=("159915",),
        pcf_paths={"159915": "sample.xml"},
    )
    try:
        write_job_state(
            controller.state_path(job.trade_date),
            {
                "status": "retry_wait",
                "pid": os.getpid(),
            },
        )

        with pytest.raises(RuntimeError, match="采集任务已经在运行"):
            controller.start(job)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_pid_probe_never_signals_current_process_on_windows(monkeypatch) -> None:
    if os.name == "nt":
        monkeypatch.setattr(
            os,
            "kill",
            lambda *_args: pytest.fail("Windows PID probe must not call os.kill"),
        )

    assert _pid_exists(os.getpid())


def test_monitor_worker_forces_utf8_output(monkeypatch) -> None:
    root = Path("tmp") / "tests" / uuid4().hex
    captured = {}

    class FakeProcess:
        pid = 12345

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("etf_arbitrage.operations.market_day.subprocess.Popen", fake_popen)
    try:
        controller = MarketMonitorController(root)
        job = MarketMonitorJob(
            trade_date="20260723",
            etf_codes=("159915",),
            pcf_paths={"159915": "sample.xml"},
        )

        controller.start(job)

        environment = captured["kwargs"]["env"]
        assert environment["PYTHONUNBUFFERED"] == "1"
        assert environment["PYTHONIOENCODING"] == "utf-8"
        assert environment["PYTHONUTF8"] == "1"
        job_path = root / "tmp" / "runtime" / "market_monitor_20260723_job.json"
        payload = json.loads(job_path.read_text(encoding="utf-8"))
        assert payload["auto_restart"] is True
        assert payload["restart_delay"] == 5.0
        assert payload["max_restart_delay"] == 60.0
        assert payload["capture_only"] is False
    finally:
        shutil.rmtree(root, ignore_errors=True)
