"""Cross-border ETF market-data contract and indicative calculations.

The module deliberately contains no public-data downloader.  Production-like
research is fed by the intranet Redis quotation hash or by normalized JSONL
recordings produced from that hash.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional, Tuple

from etf_arbitrage.data.pcf import PCFComponent, PCFDocument, SubstituteFlag
from etf_arbitrage.domain import Exchange, InstrumentId

from .models import DataSourceHealth, FXQuote, MarketSnapshot
from .source import MarketDataSource


class CrossBorderSnapshotSourceMode(str, Enum):
    """Supported first-party/local inputs for cross-border research."""

    UNAVAILABLE = "UNAVAILABLE"
    REDIS = "REDIS"
    JSONL = "JSONL"


CROSS_BORDER_SNAPSHOT_REQUIRED_FIELDS = (
    "snapshot_timestamp",
    "etf_order_book (SZSE/SSE)",
    "component_order_books (HKEX)",
    "fx_quotes.HKD/CNY.bid",
    "fx_quotes.HKD/CNY.ask",
    "fx_quotes.HKD/CNY.exchange_timestamp",
    "fx_quotes.HKD/CNY.receive_timestamp",
    "official_iopv (optional)",
    "pcf_version",
    "data_quality_status",
    "source_mode",
)


@dataclass(frozen=True)
class CrossBorderSnapshotInterface:
    """Redis/JSONL contract used by the independent cross-border console."""

    etf_code: str = "159920"
    etf_exchange: Exchange = Exchange.SZSE
    redis_hash_template: str = "{trade_date}"
    redis_etf_field_template: str = "{etf_code}.SZ"
    redis_hk_field_template: str = "{component_code}.HK"
    jsonl_path_template: str = (
        "tmp/recorded/{trade_date}/{etf_code}_observations.jsonl"
    )
    required_fields: tuple[str, ...] = CROSS_BORDER_SNAPSHOT_REQUIRED_FIELDS

    @property
    def instrument_id(self) -> InstrumentId:
        return InstrumentId(self.etf_exchange, self.etf_code)


HKDCNYQuote = FXQuote


@dataclass(frozen=True)
class CrossBorderIndicativeMetrics:
    status: str
    creation_reference_iopv_cny: Optional[float]
    redemption_reference_iopv_cny: Optional[float]
    internal_mid_iopv_cny: Optional[float]
    upstream_official_iopv_cny: Optional[float]
    etf_bid_cny: Optional[float]
    etf_ask_cny: Optional[float]
    creation_reference_basis_bps: Optional[float]
    redemption_reference_basis_bps: Optional[float]
    hkd_cny_bid: Optional[float]
    hkd_cny_ask: Optional[float]
    component_ask_coverage: float
    component_bid_coverage: float
    blockers: Tuple[str, ...]


class PendingCrossBorderMarketDataSource(MarketDataSource):
    """Non-fabricating placeholder until the intranet field map is validated."""

    def __init__(
        self,
        interface: Optional[CrossBorderSnapshotInterface] = None,
        mode: CrossBorderSnapshotSourceMode = CrossBorderSnapshotSourceMode.UNAVAILABLE,
    ) -> None:
        self.interface = interface or CrossBorderSnapshotInterface()
        self.mode = mode
        self._symbols: set[str] = set()

    def connect(self) -> None:
        return None

    def disconnect(self) -> None:
        return None

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def get_latest_snapshot(self) -> MarketSnapshot:
        return self.step()

    def step(self) -> MarketSnapshot:
        raise RuntimeError(
            "Cross-border Redis fields have not been validated on this device"
        )

    def subscribe(self, symbols: Iterable[str]) -> None:
        self._symbols.update(str(symbol) for symbol in symbols)

    def health(self) -> DataSourceHealth:
        if self.mode == CrossBorderSnapshotSourceMode.UNAVAILABLE:
            message = "跨境行情源尚未配置"
        else:
            message = "{}为主数据路径；等待在内网设备验证字段映射".format(
                self.mode.value
            )
        return DataSourceHealth(
            connected=False,
            running=False,
            status="FIELD_MAP_PENDING",
            message=message,
        )


def cross_border_hk_components(pcf: PCFDocument) -> tuple[PCFComponent, ...]:
    return tuple(
        component
        for component in pcf.components
        if component.instrument_id.exchange == Exchange.HKEX
        and not is_virtual_subscription_cash(component)
    )


def virtual_subscription_cash_component(
    pcf: PCFDocument,
) -> Optional[PCFComponent]:
    return next(
        (
            component
            for component in pcf.components
            if is_virtual_subscription_cash(component)
        ),
        None,
    )


def is_virtual_subscription_cash(component: PCFComponent) -> bool:
    symbol = str(component.symbol).replace(" ", "")
    return component.stock_code == "159900" or symbol == "申赎现金"


def calculate_cross_border_indicative_metrics(
    snapshot: MarketSnapshot,
    pcf: PCFDocument,
    fx_quote: Optional[FXQuote] = None,
) -> CrossBorderIndicativeMetrics:
    """Calculate conservative directional IOPV references in CNY.

    The virtual ``159900 申赎现金`` record is excluded because it is a
    settlement helper rather than a portfolio security.
    """

    resolved_fx = fx_quote or snapshot.hkd_cny_quote
    components = cross_border_hk_components(pcf)
    total = len(components) or 1
    creation_value_cny = pcf.estimate_cash_component
    redemption_value_cny = pcf.estimate_cash_component
    ask_count = 0
    bid_count = 0
    for component in components:
        book = snapshot.component_order_books.get(component.stock_code)
        if (
            resolved_fx is not None
            and book is not None
            and book.best_ask is not None
        ):
            creation_value_cny += (
                component.component_share * book.best_ask * resolved_fx.ask
            )
            ask_count += 1
        if (
            resolved_fx is not None
            and book is not None
            and book.best_bid is not None
        ):
            redemption_value_cny += (
                component.component_share * book.best_bid * resolved_fx.bid
            )
            bid_count += 1

    mandatory_cash = tuple(
        component
        for component in pcf.components
        if component.substitute_flag == SubstituteFlag.MANDATORY
        and not is_virtual_subscription_cash(component)
        and component.instrument_id.exchange != Exchange.HKEX
    )
    for component in mandatory_cash:
        creation_value_cny += component.creation_cash_substitute
        redemption_value_cny += component.redemption_cash_substitute

    ask_coverage = ask_count / total
    bid_coverage = bid_count / total
    creation_iopv = (
        creation_value_cny / pcf.creation_redemption_unit
        if components and ask_count == len(components)
        else None
    )
    redemption_iopv = (
        redemption_value_cny / pcf.creation_redemption_unit
        if components and bid_count == len(components)
        else None
    )
    complete_values = [
        value for value in (creation_iopv, redemption_iopv) if value is not None
    ]
    internal_mid = (
        sum(complete_values) / len(complete_values) if complete_values else None
    )
    etf_bid = snapshot.etf_order_book.best_bid
    etf_ask = snapshot.etf_order_book.best_ask
    creation_basis = (
        (etf_bid / creation_iopv - 1.0) * 10_000.0
        if etf_bid is not None and creation_iopv is not None and creation_iopv > 0
        else None
    )
    redemption_basis = (
        (redemption_iopv / etf_ask - 1.0) * 10_000.0
        if etf_ask is not None
        and redemption_iopv is not None
        and etf_ask > 0
        else None
    )
    blockers = list(snapshot.assembly_blockers)
    if resolved_fx is None:
        blockers.append("MISSING_HKD_CNY_QUOTE")
    if ask_count < len(components):
        blockers.append("INCOMPLETE_COMPONENT_ASK_COVERAGE")
    if bid_count < len(components):
        blockers.append("INCOMPLETE_COMPONENT_BID_COVERAGE")
    if etf_bid is None or etf_ask is None:
        blockers.append("MISSING_MAINLAND_ETF_TWO_SIDED_QUOTE")
    if not components:
        blockers.append("NO_HKEX_COMPONENTS_IN_PCF")
    return CrossBorderIndicativeMetrics(
        status="INDICATIVE",
        creation_reference_iopv_cny=creation_iopv,
        redemption_reference_iopv_cny=redemption_iopv,
        internal_mid_iopv_cny=internal_mid,
        upstream_official_iopv_cny=snapshot.official_iopv,
        etf_bid_cny=etf_bid,
        etf_ask_cny=etf_ask,
        creation_reference_basis_bps=creation_basis,
        redemption_reference_basis_bps=redemption_basis,
        hkd_cny_bid=resolved_fx.bid if resolved_fx is not None else None,
        hkd_cny_ask=resolved_fx.ask if resolved_fx is not None else None,
        component_ask_coverage=ask_coverage,
        component_bid_coverage=bid_coverage,
        blockers=tuple(dict.fromkeys(blockers)),
    )
