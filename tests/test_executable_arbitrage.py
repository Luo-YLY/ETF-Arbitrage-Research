from datetime import date
from pathlib import Path

from etf_arbitrage.arbitrage import ExecutableArbitrageDetector
from etf_arbitrage.data import SZSEPCFParser, validate_executable_pcf
from etf_arbitrage.executable_config import (
    CostConfig,
    DirectionSelection,
    ExecutionConfig,
    SimulationConfig,
    SimulationScenario,
)
from etf_arbitrage.market_data import SimulatedMarketDataSource


PCF = Path("data/pcf/20260722/pcf_159915_20260722.xml")


def evaluation(
    scenario,
    shock=40,
    costs=0,
    haircut=1.0,
    direction=DirectionSelection.BOTH,
):
    pcf = SZSEPCFParser().parse(PCF)
    snapshot = SimulatedMarketDataSource(
        pcf,
        SimulationConfig(scenario=scenario, premium_shock_bps=shock),
    ).step()
    detector = ExecutableArbitrageDetector(
        pcf,
        ExecutionConfig(
            direction=direction,
            depth_haircut=haircut,
            safety_buffer_bps=0,
            minimum_profit_bps=0,
            minimum_profit_amount=0,
        ),
        CostConfig(secondary_market_bps=costs),
    )
    return detector.evaluate(snapshot)


def test_premium_and_discount_generate_correct_direction():
    premium = evaluation(SimulationScenario.PREMIUM_SHOCK)
    assert premium.creation.executable
    assert premium.creation.net_profit > 0
    assert not premium.redemption.executable

    discount = evaluation(SimulationScenario.DISCOUNT_SHOCK)
    assert discount.redemption.executable
    assert discount.redemption.net_profit > 0
    assert not discount.creation.executable


def test_costs_and_depth_can_remove_opportunity():
    expensive = evaluation(SimulationScenario.PREMIUM_SHOCK, shock=10, costs=20)
    assert not expensive.creation.executable
    shallow = evaluation(SimulationScenario.COMPONENT_DEPTH_SHORTAGE)
    assert "COMPONENT_ASK_DEPTH" in shallow.creation.rejection_reasons


def test_bounds_capacity_and_pcf_validation():
    result = evaluation(SimulationScenario.PREMIUM_SHOCK)
    assert result.lower_bound < result.upper_bound
    assert result.creation_capacity.max_executable_cu == 1
    _, report = validate_executable_pcf(PCF, "159915")
    assert report.valid
    assert report.component_count == 100
    assert len(report.file_hash) == 64


def test_pcf_trading_date_mismatch_is_invalid():
    _, report = validate_executable_pcf(PCF, "159915", date(2026, 7, 23))
    assert not report.valid
    assert "TRADING_DATE_MISMATCH" in report.errors


def test_direction_filter_is_explicit_rejection():
    result = evaluation(
        SimulationScenario.PREMIUM_SHOCK,
        direction=DirectionSelection.REDEMPTION_ONLY,
    )
    assert not result.creation.executable
    assert "DIRECTION_DISABLED" in result.creation.rejection_reasons
