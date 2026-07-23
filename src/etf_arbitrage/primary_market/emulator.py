"""Deterministic configurable ETF primary-market emulator."""

from __future__ import annotations

from datetime import datetime, timedelta
import random
from uuid import uuid4

from etf_arbitrage.data.pcf import PCFDocument
from etf_arbitrage.executable_config import ArbitrageDirection

from .models import PrimaryMarketRequest, PrimaryMarketStatus


class PrimaryMarketEmulator:
    def __init__(
        self,
        confirmation_latency_ms: int = 500,
        rejection_probability: float = 0.0,
        cash_component_error: float = 0.0,
        final_cash_delay_ms: int = 1_000,
        random_seed: int = 42,
    ) -> None:
        if not 0 <= rejection_probability <= 1:
            raise ValueError("rejection_probability must be in [0, 1]")
        self.confirmation_latency_ms = confirmation_latency_ms
        self.rejection_probability = rejection_probability
        self.cash_component_error = cash_component_error
        self.final_cash_delay_ms = final_cash_delay_ms
        self._random = random.Random(random_seed)

    def submit(
        self,
        cycle_id: str,
        pcf: PCFDocument,
        direction: ArbitrageDirection,
        cu_count: int,
        submit_time: datetime,
    ) -> PrimaryMarketRequest:
        request_id = "pm-{}".format(uuid4().hex[:12])
        history = [
            PrimaryMarketStatus.REQUEST_CREATED.value,
            PrimaryMarketStatus.REQUEST_SUBMITTED.value,
        ]
        if self._random.random() < self.rejection_probability:
            history.append(PrimaryMarketStatus.REQUEST_REJECTED.value)
            return PrimaryMarketRequest(
                request_id=request_id,
                cycle_id=cycle_id,
                etf_code=pcf.etf_code,
                direction=direction.value,
                cu_count=cu_count,
                submit_time=submit_time,
                confirm_time=None,
                final_settlement_time=None,
                pcf_date=pcf.trading_day,
                estimated_cash_component=pcf.estimate_cash_component,
                final_cash_component=pcf.estimate_cash_component,
                status=PrimaryMarketStatus.REQUEST_REJECTED,
                reject_reason="SIMULATED_PRIMARY_MARKET_REJECTION",
                status_history=tuple(history),
            )
        confirm = submit_time + timedelta(milliseconds=self.confirmation_latency_ms)
        settled = confirm + timedelta(milliseconds=self.final_cash_delay_ms)
        history.extend(
            [
                PrimaryMarketStatus.REQUEST_ACCEPTED.value,
                PrimaryMarketStatus.REQUEST_CONFIRMED.value,
                PrimaryMarketStatus.FINAL_CASH_SETTLED.value,
            ]
        )
        return PrimaryMarketRequest(
            request_id=request_id,
            cycle_id=cycle_id,
            etf_code=pcf.etf_code,
            direction=direction.value,
            cu_count=cu_count,
            submit_time=submit_time,
            confirm_time=confirm,
            final_settlement_time=settled,
            pcf_date=pcf.trading_day,
            estimated_cash_component=pcf.estimate_cash_component,
            final_cash_component=pcf.estimate_cash_component + self.cash_component_error,
            status=PrimaryMarketStatus.FINAL_CASH_SETTLED,
            reject_reason=None,
            status_history=tuple(history),
        )
