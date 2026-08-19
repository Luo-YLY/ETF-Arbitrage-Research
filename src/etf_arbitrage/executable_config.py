"""Strongly typed settings for the executable arbitrage simulator."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict


class DataSourceMode(str, Enum):
    SIMULATED = "SIMULATED"
    FILE_REPLAY = "FILE_REPLAY"
    REDIS = "REDIS"


class RedisSnapshotFormat(str, Enum):
    NORMALIZED_JSON = "NORMALIZED_JSON"
    DATE_HASH = "DATE_HASH"


class SimulationScenario(str, Enum):
    NORMAL = "NORMAL"
    PREMIUM_SHOCK = "PREMIUM_SHOCK"
    DISCOUNT_SHOCK = "DISCOUNT_SHOCK"
    MEAN_REVERSION = "MEAN_REVERSION"
    ETF_DEPTH_SHORTAGE = "ETF_DEPTH_SHORTAGE"
    COMPONENT_DEPTH_SHORTAGE = "COMPONENT_DEPTH_SHORTAGE"
    STALE_QUOTE = "STALE_QUOTE"
    MISSING_QUOTE = "MISSING_QUOTE"
    SUSPENSION = "SUSPENSION"
    LIMIT_UP_NO_ASK = "LIMIT_UP_NO_ASK"
    LIMIT_DOWN_NO_BID = "LIMIT_DOWN_NO_BID"
    DECODE_ERROR = "DECODE_ERROR"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    CROSSED_BOOK = "CROSSED_BOOK"
    PCF_INVALID = "PCF_INVALID"


class SimulationPriceSeedMode(str, Enum):
    AUTO_LOCAL = "AUTO_LOCAL"
    LOCAL_RECORDING = "LOCAL_RECORDING"
    SYNTHETIC = "SYNTHETIC"
    REDIS_LATEST = "REDIS_LATEST"


class ExecutionMode(str, Enum):
    INVENTORY_LOCKED = "INVENTORY_LOCKED"
    SEQUENTIAL_NO_BORROW = "SEQUENTIAL_NO_BORROW"


class ExecutionScenario(str, Enum):
    OPTIMISTIC = "OPTIMISTIC"
    BASE = "BASE"
    STRESS = "STRESS"


class ArbitrageDirection(str, Enum):
    CREATION = "CREATION"
    REDEMPTION = "REDEMPTION"


class DirectionSelection(str, Enum):
    BOTH = "BOTH"
    CREATION_ONLY = "CREATION_ONLY"
    REDEMPTION_ONLY = "REDEMPTION_ONLY"


@dataclass(frozen=True)
class RedisConfig:
    enabled: bool = False
    host: str = "localhost"
    port: int = 6379
    db: int = 0
    password: str | None = None
    snapshot_format: RedisSnapshotFormat = RedisSnapshotFormat.NORMALIZED_JSON
    trade_date_key: str = ""
    hkd_cny_code: str = ""
    number_of_book_levels: int = 5
    poll_interval_ms: int = 3_000
    recording_path: str = ""
    key_prefix: str = "etf_arbitrage"
    channel_pattern: str = "market:*"
    socket_timeout_seconds: float = 2.0


@dataclass(frozen=True)
class SimulationConfig:
    scenario: SimulationScenario = SimulationScenario.NORMAL
    random_seed: int = 42
    price_seed_mode: SimulationPriceSeedMode = SimulationPriceSeedMode.AUTO_LOCAL
    local_recording_path: str = ""
    redis_code_suffix: str = ".SZ"
    redis_hkd_cny_code: str = ""
    hkd_cny_mid: float = 0.92
    hkd_cny_spread_bps: float = 2.0
    hkd_cny_volatility: float = 0.0
    tick_interval_ms: int = 1_000
    total_ticks: int = 300
    simulation_speed: float = 1.0
    base_volatility: float = 0.00015
    etf_spread_bps: float = 2.0
    component_spread_bps: float = 4.0
    number_of_book_levels: int = 5
    depth_per_level: float = 2.0
    depth_decay: float = 0.85
    premium_shock_bps: float = 35.0
    shock_start_tick: int = 0
    shock_duration_ticks: int = 30
    mean_reversion_speed: float = 0.12
    stale_quote_ratio: float = 0.10
    missing_quote_ratio: float = 0.05
    suspended_weight: float = 0.05
    limit_up_weight: float = 0.05
    limit_down_weight: float = 0.05
    quote_latency_ms: int = 50
    sequence_gap_probability: float = 0.0


@dataclass(frozen=True)
class FileReplayConfig:
    path: str = ""
    fixed_step_ms: int = 1_000
    playback_speed: float = 1.0
    use_real_intervals: bool = False
    schema_mapping: Dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CostConfig:
    secondary_market_bps: float = 3.0
    creation_fee_bps: float = 0.0
    redemption_fee_bps: float = 0.0
    borrowing_bps: float = 0.0
    financing_bps: float = 0.0


@dataclass(frozen=True)
class ExecutionConfig:
    direction: DirectionSelection = DirectionSelection.BOTH
    mode: ExecutionMode = ExecutionMode.INVENTORY_LOCKED
    scenario: ExecutionScenario = ExecutionScenario.BASE
    cu_count: int = 1
    maximum_cu_per_trade: int = 1
    maximum_daily_cu: int = 10
    decision_latency_ms: int = 50
    order_latency_ms: int = 50
    primary_market_latency_ms: int = 500
    depth_haircut: float = 0.90
    safety_buffer_bps: float = 3.0
    minimum_profit_bps: float = 1.0
    minimum_profit_amount: float = 100.0
    optional_cash_substitution: bool = False
    auto_paper_trade: bool = False
    record_opportunities_only: bool = True


@dataclass(frozen=True)
class DataQualityConfig:
    max_quote_age_ms: int = 5_000
    max_cross_section_skew_ms: int = 5_000
    maximum_missing_weight: float = 0.01
    maximum_stale_weight: float = 0.05
    maximum_suspended_weight: float = 0.05
    maximum_limit_up_weight: float = 0.10
    maximum_limit_down_weight: float = 0.10
    maximum_iopv_error_bps: float = 30.0
    kill_switch_enabled: bool = True


@dataclass(frozen=True)
class AccountConfig:
    initial_cash: float = 50_000_000.0
    initial_etf_inventory: int = 0
    etf_borrow_limit: int = 10_000_000
    allow_stock_borrow: bool = True


@dataclass(frozen=True)
class PrimaryMarketConfig:
    rejection_probability: float = 0.0
    cash_component_error: float = 0.0
    final_cash_delay_ms: int = 1_000


@dataclass(frozen=True)
class PaperArbitrageConfig:
    etf_code: str = "159915"
    data_source: DataSourceMode = DataSourceMode.SIMULATED
    redis: RedisConfig = field(default_factory=RedisConfig)
    simulation: SimulationConfig = field(default_factory=SimulationConfig)
    file_replay: FileReplayConfig = field(default_factory=FileReplayConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    quality: DataQualityConfig = field(default_factory=DataQualityConfig)
    account: AccountConfig = field(default_factory=AccountConfig)
    primary_market: PrimaryMarketConfig = field(default_factory=PrimaryMarketConfig)
    save_run_data: bool = True

    def to_dict(self, include_secrets: bool = False) -> Dict[str, Any]:
        def convert(value: Any) -> Any:
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, dict):
                return {key: convert(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [convert(item) for item in value]
            return value

        payload = convert(asdict(self))
        if not include_secrets and payload["redis"].get("password"):
            payload["redis"]["password"] = "***"
        return payload
