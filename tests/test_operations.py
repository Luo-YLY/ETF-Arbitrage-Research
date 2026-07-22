from datetime import datetime
from io import BytesIO
from pathlib import Path
import shutil
from uuid import uuid4
from zipfile import ZipFile

import pandas as pd
import pytest

from etf_arbitrage.data import PCFRepository, PCFValidationError
from etf_arbitrage.operations import (
    MarketMonitorController,
    MarketMonitorJob,
    SZSEMarketSchedule,
)
from etf_arbitrage.dashboard.app import _resample_last


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
    assert candidates[1].endswith("pcf_159915_20260722.txt")
    assert candidates[-2].endswith("159915ETF20260722.xml")
    assert candidates[-1].endswith("159915ETF20260722.txt")


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
