from dataclasses import replace
from datetime import date
from math import isnan
from pathlib import Path

from etf_arbitrage.arbitrage import ExecutableArbitrageDetector
from etf_arbitrage.data import (
    SZSEPCFParser,
    SubstituteFlag,
    validate_executable_pcf,
)
from etf_arbitrage.executable_config import (
    CostConfig,
    DirectionSelection,
    ExecutionConfig,
    SimulationConfig,
    SimulationScenario,
)
from etf_arbitrage.market_data import (
    InstrumentState,
    SimulatedMarketDataSource,
    TradingStatus,
)
from etf_arbitrage.pricing import (
    ComponentExecutionAction,
    ExecutableBasketPricer,
)


PCF = Path("data/pcf/20260722/pcf_159915_20260722.xml")


def evaluation(
    scenario,
    shock=40,
    costs=0,
    haircut=1.0,
    direction=DirectionSelection.BOTH,
    optional_cash=False,
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
            optional_cash_substitution=optional_cash,
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


def test_incomplete_depth_never_emits_partial_basket_profit_or_bounds():
    shallow = evaluation(SimulationScenario.COMPONENT_DEPTH_SHORTAGE)

    assert not shallow.creation.pricing_complete
    assert not shallow.redemption.pricing_complete
    assert isnan(shallow.creation.gross_profit)
    assert isnan(shallow.creation.estimated_costs)
    assert isnan(shallow.creation.net_profit)
    assert isnan(shallow.creation.net_profit_bps)
    assert isnan(shallow.redemption.net_profit)
    assert isnan(shallow.redemption.net_profit_bps)
    assert isnan(shallow.lower_bound)
    assert isnan(shallow.upper_bound)
    assert shallow.creation_capacity.marginal_profits == ()
    assert shallow.redemption_capacity.marginal_profits == ()


def test_adaptive_cash_substitution_only_replaces_failed_allowed_component():
    result = evaluation(
        SimulationScenario.COMPONENT_DEPTH_SHORTAGE,
        optional_cash=True,
    )

    creation = result.creation.basket
    redemption = result.redemption.basket
    assert creation.fully_filled
    assert redemption.fully_filled
    assert len(creation.adaptive_cash_substituted_symbols) == 1
    assert len(redemption.adaptive_cash_substituted_symbols) == 1
    symbol = creation.adaptive_cash_substituted_symbols[0]
    assert creation.component_plans[symbol].action == (
        ComponentExecutionAction.CASH_ADAPTIVE
    )
    assert creation.optional_cash_ratio <= creation.max_cash_ratio
    assert len(creation.cash_substituted_symbols) < len(creation.component_plans)


def test_limit_lock_is_directional_and_replans_to_physical_after_unlock():
    pcf = SZSEPCFParser().parse(PCF)
    snapshot = SimulatedMarketDataSource(
        pcf,
        SimulationConfig(scenario=SimulationScenario.NORMAL),
    ).step()
    symbol = next(
        component.stock_code
        for component in pcf.components
        if component.component_share > 0
        and component.substitute_flag.name == "ALLOWED"
    )
    normal_book = snapshot.component_order_books[symbol]
    locked_book = replace(
        normal_book,
        asks=(),
        trading_status=TradingStatus.LIMIT_UP,
        instrument_state=InstrumentState.LIMIT_UP_LOCKED,
    )
    locked_books = dict(snapshot.component_order_books)
    locked_books[symbol] = locked_book
    pricer = ExecutableBasketPricer(pcf, optional_cash_substitution=True)

    creation_locked = pricer.creation_cost(locked_books)
    redemption_locked = pricer.redemption_proceeds(locked_books)
    creation_unlocked = pricer.creation_cost(snapshot.component_order_books)

    assert creation_locked.component_plans[symbol].action == (
        ComponentExecutionAction.CASH_ADAPTIVE
    )
    assert creation_locked.component_plans[symbol].reason == "LIMIT_UP_LOCKED"
    assert redemption_locked.component_plans[symbol].action == (
        ComponentExecutionAction.PHYSICAL
    )
    assert creation_unlocked.component_plans[symbol].action == (
        ComponentExecutionAction.PHYSICAL
    )
    assert symbol not in creation_unlocked.adaptive_cash_substituted_symbols


def test_all_component_bottlenecks_are_reported_in_one_snapshot():
    pcf = SZSEPCFParser().parse(PCF)
    snapshot = SimulatedMarketDataSource(
        pcf,
        SimulationConfig(scenario=SimulationScenario.NORMAL),
    ).step()
    symbols = [
        component.stock_code
        for component in pcf.components
        if component.component_share > 0
        and component.substitute_flag.name == "ALLOWED"
    ][:2]
    books = dict(snapshot.component_order_books)
    for symbol in symbols:
        books[symbol] = replace(
            books[symbol],
            asks=(),
            trading_status=TradingStatus.LIMIT_UP,
            instrument_state=InstrumentState.LIMIT_UP_LOCKED,
        )

    basket = ExecutableBasketPricer(pcf).creation_cost(books)

    assert not basket.fully_filled
    assert basket.bottleneck_symbols == tuple(symbols)
    assert all(
        basket.component_plans[symbol].action == ComponentExecutionAction.BLOCKED
        for symbol in symbols
    )


def test_virtual_subscription_cash_is_not_double_counted_in_iopv_or_basket():
    original = SZSEPCFParser().parse(PCF)
    snapshot = SimulatedMarketDataSource(
        original,
        SimulationConfig(scenario=SimulationScenario.NORMAL),
    ).step()
    virtual_cash = replace(
        original.components[0],
        stock_code="159900",
        symbol="申赎现金",
        component_share=0.0,
        substitute_flag=SubstituteFlag.MANDATORY,
        creation_cash_substitute=935_000.0,
        redemption_cash_substitute=934_000.0,
    )
    with_virtual_cash = replace(
        original,
        components=(*original.components, virtual_cash),
        record_num=original.record_num + 1,
        total_record_num=original.total_record_num + 1,
    )
    baseline = ExecutableBasketPricer(original)
    corrected = ExecutableBasketPricer(with_virtual_cash)

    assert corrected.internal_iopv(snapshot.component_order_books) == (
        baseline.internal_iopv(snapshot.component_order_books)
    )
    assert corrected.creation_cost(snapshot.component_order_books).total_value == (
        baseline.creation_cost(snapshot.component_order_books).total_value
    )
    assert corrected.redemption_proceeds(
        snapshot.component_order_books
    ).total_value == baseline.redemption_proceeds(
        snapshot.component_order_books
    ).total_value
    assert "159900" not in corrected.creation_cost(
        snapshot.component_order_books
    ).component_plans


def test_pcf_maximum_cash_ratio_is_a_fail_closed_gate():
    original = SZSEPCFParser().parse(PCF)
    pcf = replace(original, max_cash_ratio=0.01)
    snapshot = SimulatedMarketDataSource(
        pcf,
        SimulationConfig(scenario=SimulationScenario.NORMAL),
    ).step()
    books = {
        symbol: replace(book, asks=())
        for symbol, book in snapshot.component_order_books.items()
    }

    basket = ExecutableBasketPricer(
        pcf, optional_cash_substitution=True
    ).creation_cost(books)

    assert not basket.fully_filled
    assert basket.optional_cash_ratio > basket.max_cash_ratio
    assert "MAX_CASH_SUBSTITUTION_RATIO_EXCEEDED" in basket.failure_reasons


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
