from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from etf_arbitrage.data import PCFDocument, SubstituteFlag
from etf_arbitrage.domain import EventType, Exchange, InstrumentId
from etf_arbitrage.exchanges import SSEAdapter, SZSEAdapter
from etf_arbitrage.strategies.event_driven import (
    compare_pcf,
    validated_pcf_event,
)


PCF = Path("data/pcf/20260722/pcf_159915_20260722.xml")
NOW = datetime(2026, 7, 22, 8, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_szse_adapter_wraps_existing_parser_with_audit_metadata() -> None:
    adapter = SZSEAdapter()

    document = adapter.parse_pcf(PCF)

    assert document.etf_id == InstrumentId(Exchange.SZSE, "159915")
    assert document.file_hash
    assert document.schema_version == document.version
    assert all(component.instrument_id.exchange == Exchange.SZSE for component in document.components)
    assert adapter.codec.encode(document.etf_id) == "159915.SZ"


def test_sse_adapter_exposes_research_pcf_parser_but_not_production_rules() -> None:
    adapter = SSEAdapter()

    assert adapter.codec.decode("510300.SH") == InstrumentId(Exchange.SSE, "510300")
    assert not adapter.capabilities.production_ready
    assert adapter.capabilities.pcf_parser
    assert adapter.pcf_parser is not None


def test_pcf_payload_round_trip_preserves_cross_market_metadata() -> None:
    document = SZSEAdapter().parse_pcf(PCF)

    restored = PCFDocument.from_dict(document.to_dict())

    assert restored == document
    assert restored.etf_id == document.etf_id
    assert restored.components[0].instrument_id == document.components[0].instrument_id


def test_pcf_diff_emits_structured_component_substitution_and_limit_events() -> None:
    previous = SZSEAdapter().parse_pcf(PCF)
    changed_component = replace(
        previous.components[0],
        component_share=previous.components[0].component_share + 100,
        substitute_flag=SubstituteFlag.MANDATORY,
    )
    current = replace(
        previous,
        version="1.1",
        file_hash="b" * 64,
        creation_allowed=False,
        net_creation_limit=1000,
        components=(changed_component,) + previous.components[1:],
    )

    diff = compare_pcf(previous, current)
    events = diff.to_events(previous, current, NOW, NOW)

    assert diff.changed
    assert {
        EventType.PCF_COMPONENT_CHANGED,
        EventType.PCF_SUBSTITUTION_RULE_CHANGED,
        EventType.PCF_CREATION_REDEMPTION_STATUS_CHANGED,
        EventType.PCF_LIMIT_CHANGED,
    }.issubset({event.event_type for event in events})
    assert all(
        event.payload["holdings_inference"] == "NOT_ESTABLISHED"
        for event in events
    )


def test_validated_pcf_event_contains_replayable_document() -> None:
    document = SZSEAdapter().parse_pcf(PCF)

    event = validated_pcf_event(document, NOW, NOW)
    restored = PCFDocument.from_dict(event.payload["pcf"])

    assert event.event_type == EventType.PCF_VALIDATED
    assert restored == document
