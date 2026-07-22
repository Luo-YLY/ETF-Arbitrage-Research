from datetime import datetime
from io import BytesIO
from pathlib import Path
import shutil
from uuid import uuid4
from zipfile import ZipFile

import pytest

from etf_arbitrage.data import PCFRepository, PCFValidationError
from etf_arbitrage.operations import (
    MarketMonitorController,
    MarketMonitorJob,
    SZSEMarketSchedule,
)


PCF_SAMPLE = Path("data/pcf/20260722/pcf_159915_20260722.xml")


def test_market_schedule_covers_sessions_and_lunch_break() -> None:
    schedule = SZSEMarketSchedule()

    assert not schedule.is_open(datetime(2026, 7, 22, 9, 29, 59))
    assert schedule.is_open(datetime(2026, 7, 22, 9, 30))
    assert schedule.is_open(datetime(2026, 7, 22, 11, 30))
    assert not schedule.is_open(datetime(2026, 7, 22, 12, 0))
    assert schedule.is_open(datetime(2026, 7, 22, 13, 0))
    assert schedule.is_open(datetime(2026, 7, 22, 15, 0))
    assert schedule.is_after_close(datetime(2026, 7, 22, 15, 0, 1))


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


def test_monitor_job_validation_and_command() -> None:
    controller = MarketMonitorController(Path.cwd())
    job = MarketMonitorJob(
        trade_date="20260722",
        etf_codes=("159915",),
        pcf_paths={"159915": "sample.xml"},
    )

    job.validate()
    command = controller.build_command(Path("job.json"))

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
