from datetime import date
from io import BytesIO
from pathlib import Path
import shutil
from uuid import uuid4
from xml.sax.saxutils import escape
from zipfile import ZipFile
import pytest

from etf_arbitrage.data import (
    HKPCFParser,
    HKPCFRepository,
    HKPCFValidationError,
)


def _inline(reference: str, value: str) -> str:
    return '<c r="{}" t="inlineStr"><is><t>{}</t></is></c>'.format(
        reference,
        escape(value),
    )


def _number(reference: str, value) -> str:
    return '<c r="{}"><v>{}</v></c>'.format(reference, value)


def _excel_serial(value: date) -> int:
    return (value - date(1899, 12, 30)).days


def _sample_workbook(basket_day: date = date(2026, 8, 4)) -> bytes:
    valuation_day = date(2026, 8, 3)
    rows = {
        1: [_inline("A1", "Trading Information of Exchange Traded Funds")],
        4: [
            _inline("A4", "Name of ETF Manager"),
            _inline("B4", "Hang Seng Investment Management Limited"),
        ],
        6: [
            _inline("A6", "Name of ETF"),
            _inline("B6", "TRACKER FUND OF HONG KONG"),
        ],
        8: [
            _inline("A8", "Stock Code"),
            _number("C8", 2800),
            _number("F8", 82800),
        ],
        10: [
            _inline("A10", "Date (ddmmmyyyy)"),
            _number("C10", _excel_serial(valuation_day)),
        ],
        13: [
            _inline("A13", "N.A.V. per Unit in Trading Currency (Note 1)"),
            _inline("B13", "HKD"),
            _number("C13", 26.4986),
            _inline("E13", "RMB"),
            _number("F13", 22.8196),
        ],
        14: [
            _inline("A14", "N.A.V. per Creation Unit (Note 2)"),
            _inline("B14", "HKD"),
            _number("C14", 26498600),
            _inline("E14", "HKD"),
            _number("F14", 26498600),
        ],
        15: [
            _inline("A15", "Actual Cash per Creation Unit (Note 3)"),
            _inline("B15", "HKD"),
            _number("C15", 374853.29),
            _inline("E15", "HKD"),
            _number("F15", 374853.29),
        ],
        26: [
            _inline("A26", "Dividend Equivalent Amount per Unit (Note 6)"),
            _inline("B26", "HKD"),
            _number("C26", 0.375),
            _inline("E26", "HKD"),
            _number("F26", 0.375),
        ],
        27: [
            _inline(
                "A27", "Dividend Equivalent Amount per Creation Unit (Note 7)"
            ),
            _inline("B27", "HKD"),
            _number("C27", 375048),
            _inline("E27", "HKD"),
            _number("F27", 375048),
        ],
        28: [
            _inline("A28", "Balancing Cash Amount per Creation Unit (Note 8)"),
            _inline("B28", "HKD"),
            _number("C28", -194.71),
            _inline("E28", "HKD"),
            _number("F28", -194.71),
        ],
        45: [
            _inline("A45", "Index Basket Shares per Creation Unit as of"),
            _number("E45", _excel_serial(basket_day)),
        ],
        47: [
            _inline("A47", "Stock Code"),
            _inline("B47", "Stock Name"),
            _inline("E47", "Quantity"),
        ],
        48: [
            _inline("A48", "0001"),
            _inline("B48", "CKH HOLDINGS"),
            _number("E48", 4119),
        ],
        49: [
            _inline("A49", "0700"),
            _inline("B49", "TENCENT"),
            _number("E49", 4346),
        ],
        50: [_inline("A50", ""), _inline("B50", ""), _inline("E50", "")],
        51: [
            _inline("A51", "Estimated Cash Component per Creation Unit:"),
            _inline("D51", "HKD"),
            _number("E51", 374853.29),
        ],
        53: [_inline("A53", "- End -")],
    }
    row_xml = "".join(
        '<row r="{}">{}</row>'.format(number, "".join(cells))
        for number, cells in sorted(rows.items())
    )
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        "<sheetData>{}</sheetData></worksheet>".format(row_xml)
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Report" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/></Relationships>'
    )
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return output.getvalue()


def test_parses_official_trahk_pcf_shape_without_excel_dependency() -> None:
    document = HKPCFParser().parse_bytes(_sample_workbook())

    assert document.etf_id.key == "HKEX:2800"
    assert document.trading_day == date(2026, 8, 4)
    assert document.previous_trading_day == date(2026, 8, 3)
    assert document.creation_redemption_unit == 1_000_000
    assert document.estimate_cash_component == pytest.approx(374853.29)
    assert document.nav == pytest.approx(26.4986)
    assert document.raw_fields["counter_nav"] == {
        "HKD": pytest.approx(26.4986),
        "RMB": pytest.approx(22.8196),
    }
    assert [component.instrument_id.key for component in document.components] == [
        "HKEX:0001",
        "HKEX:0700",
    ]
    assert not document.creation_allowed
    assert not document.redemption_allowed


def test_hk_repository_saves_both_counters_to_one_validated_pcf() -> None:
    root = Path("tmp") / "tests" / uuid4().hex
    repository = HKPCFRepository(root)
    try:
        path = repository.save(_sample_workbook(), "20260804", "82800.HK")

        assert path == root / "20260804" / "pcf_2800_20260804.xlsx"
        assert repository.find("20260804", "2800.HK") == path
        assert repository.validate(path, "20260804", "82800").trading_day == date(
            2026, 8, 4
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_hk_repository_rejects_latest_file_for_wrong_requested_day() -> None:
    repository = HKPCFRepository(Path("tmp") / "tests" / uuid4().hex)

    with pytest.raises(HKPCFValidationError, match="basket date"):
        repository.save(_sample_workbook(), "20260805", "2800")


def test_hk_repository_download_uses_official_binary_and_validates(monkeypatch) -> None:
    root = Path("tmp") / "tests" / uuid4().hex

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self):
            return _sample_workbook()

    requested = []

    def fake_urlopen(request, timeout):
        requested.append((request.full_url, timeout))
        return FakeResponse()

    monkeypatch.setattr("etf_arbitrage.data.hk_pcf.urlopen", fake_urlopen)
    try:
        path = HKPCFRepository(root).download("20260804")

        assert path.exists()
        assert requested[0][0].startswith("https://rbwm-api.hsbc.com.hk/")
        assert requested[0][1] == 30.0
    finally:
        shutil.rmtree(root, ignore_errors=True)
