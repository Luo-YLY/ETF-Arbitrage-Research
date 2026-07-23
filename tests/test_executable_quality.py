from pathlib import Path

from etf_arbitrage.data import SZSEPCFParser
from etf_arbitrage.executable_config import DataQualityConfig, SimulationConfig, SimulationScenario
from etf_arbitrage.market_data import DataQualityChecker, SimulatedMarketDataSource


PCF = Path("data/pcf/20260722/pcf_159915_20260722.xml")


def report(scenario, **quality):
    pcf = SZSEPCFParser().parse(PCF)
    snapshot = SimulatedMarketDataSource(
        pcf, SimulationConfig(scenario=scenario, stale_quote_ratio=0.20)
    ).step()
    return DataQualityChecker(DataQualityConfig(**quality)).evaluate(
        snapshot, pcf, snapshot.internal_iopv
    )


def test_stale_quotes_block_without_silent_ignore():
    result = report(SimulationScenario.STALE_QUOTE, maximum_stale_weight=0.01)
    assert not result.new_trades_enabled
    assert "STALE_COMPONENT_QUOTES" in result.blockers


def test_missing_quotes_block_without_silent_ignore():
    result = report(SimulationScenario.MISSING_QUOTE, maximum_missing_weight=0.01)
    assert not result.new_trades_enabled
    assert "MISSING_COMPONENT_QUOTES" in result.blockers


def test_decode_sequence_and_crossed_books_trigger_kill_switch():
    assert report(SimulationScenario.DECODE_ERROR).kill_switch
    assert report(SimulationScenario.SEQUENCE_GAP).kill_switch
    assert report(SimulationScenario.CROSSED_BOOK).kill_switch


def test_limit_scenarios_block_the_affected_direction():
    up = report(SimulationScenario.LIMIT_UP_NO_ASK, maximum_limit_up_weight=0.0)
    down = report(SimulationScenario.LIMIT_DOWN_NO_BID, maximum_limit_down_weight=0.0)
    assert "LIMIT_UP_NO_ASK_WEIGHT" in up.blockers
    assert "LIMIT_DOWN_NO_BID_WEIGHT" in down.blockers
