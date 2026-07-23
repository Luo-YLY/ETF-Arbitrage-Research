from pathlib import Path

from etf_arbitrage.data import SZSEPCFParser
from etf_arbitrage.engine import PaperArbitrageEngine
from etf_arbitrage.executable_config import (
    ExecutionConfig,
    PaperArbitrageConfig,
    SimulationConfig,
    SimulationScenario,
)
from etf_arbitrage.market_data import SimulatedMarketDataSource


PCF = Path("data/pcf/20260722/pcf_159915_20260722.xml")


def run_cycle(scenario):
    pcf = SZSEPCFParser().parse(PCF)
    config = PaperArbitrageConfig(
        simulation=SimulationConfig(
            scenario=scenario,
            premium_shock_bps=40,
            tick_interval_ms=1_000,
        ),
        execution=ExecutionConfig(
            auto_paper_trade=True,
            record_opportunities_only=False,
            depth_haircut=1.0,
            safety_buffer_bps=0,
            minimum_profit_bps=0,
            minimum_profit_amount=0,
        ),
    )
    source = SimulatedMarketDataSource(pcf, config.simulation)
    decision = source.step()
    fill = source.step()
    engine = PaperArbitrageEngine(pcf, config)
    return engine, engine.process(decision, fill)


def test_premium_end_to_end_closes_creation_cycle():
    engine, result = run_cycle(SimulationScenario.PREMIUM_SHOCK)
    assert result.cycle.state.value == "CLOSED"
    assert result.cycle.direction == "CREATION"
    assert result.cycle.final_pnl > 0
    assert result.primary_request.status.value == "FINAL_CASH_SETTLED"
    assert engine.account.realized_pnl == result.cycle.final_pnl
    assert all(order.simulated_only for order in result.cycle.orders)


def test_discount_end_to_end_closes_redemption_cycle():
    _, result = run_cycle(SimulationScenario.DISCOUNT_SHOCK)
    assert result.cycle.state.value == "CLOSED"
    assert result.cycle.direction == "REDEMPTION"
    assert result.cycle.final_pnl > 0


def test_same_time_book_cannot_fill_latency_sensitive_cycle():
    pcf = SZSEPCFParser().parse(PCF)
    config = PaperArbitrageConfig(
        simulation=SimulationConfig(scenario=SimulationScenario.PREMIUM_SHOCK),
        execution=ExecutionConfig(
            auto_paper_trade=True,
            record_opportunities_only=False,
            depth_haircut=1,
            safety_buffer_bps=0,
            minimum_profit_bps=0,
            minimum_profit_amount=0,
        ),
    )
    snapshot = SimulatedMarketDataSource(pcf, config.simulation).step()
    result = PaperArbitrageEngine(pcf, config).process(snapshot, snapshot)
    assert result.cycle.state.value == "REJECTED"
    assert result.cycle.rejection_reason == "NO_POST_LATENCY_SNAPSHOT"
