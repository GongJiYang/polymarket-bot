"""Typed boundaries shared by runners, strategies, and adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from polymarket_bot.microstructure.models import MarketMetadata, OrderBook


@dataclass(frozen=True, slots=True)
class FeeMetadata:
    rate: Decimal
    exponent: Decimal
    minimum_size: Decimal


@dataclass(frozen=True, slots=True)
class StrategyContext:
    market: MarketMetadata
    up_book: OrderBook
    down_book: OrderBook
    fee: FeeMetadata
    source: str
    opening: Decimal
    prices: tuple[tuple[datetime, Decimal], ...]
    current_spot: tuple[datetime, Decimal]
    threshold: Decimal
    quantity: Decimal
    target_all_in_debit: Decimal | None = None
    observed_at: tuple[tuple[str, datetime], ...] = ()


class ExecutionPreparationRejected(RuntimeError):
    """A proven pre-POST candidate rejection that is safe to monitor past."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class StrategyCandidate:
    strategy_id: str
    direction: str
    token_id: str
    book: OrderBook
    source: str
    opening: Decimal
    prices: tuple[tuple[datetime, Decimal], ...]
    terminal_probability: Decimal
    volatility_per_sqrt_second: Decimal | None
    top_ask: Decimal
    max_price: Decimal
    expected_fill_price: Decimal
    fee_per_share: Decimal
    net_edge: Decimal
    signal_metric: str
    signal_strength: Decimal
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.strategy_id.strip() or not self.token_id.strip():
            raise ValueError("strategy and token identities are required")
        if self.direction not in {"up", "down"}:
            raise ValueError("direction must be up or down")
        for name in (
            "terminal_probability",
            "top_ask",
            "max_price",
            "expected_fill_price",
            "fee_per_share",
            "net_edge",
            "signal_strength",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal")
        if not Decimal("0.5") < self.terminal_probability <= Decimal("1"):
            raise ValueError(
                "terminal_probability must identify the model-favored outcome"
            )
        if not Decimal("0") < self.expected_fill_price <= self.max_price < Decimal("1"):
            raise ValueError(
                "expected_fill_price and max_price must be ordered between zero and one"
            )
        if self.fee_per_share < Decimal("0"):
            raise ValueError("fee_per_share must not be negative")
        expected_edge = (
            self.terminal_probability
            - self.expected_fill_price
            - self.fee_per_share
        )
        if self.net_edge != expected_edge:
            raise ValueError(
                "net_edge must equal terminal_probability - "
                "expected_fill_price - fee_per_share"
            )

MetricValue = Decimal | int | str | bool | None


@dataclass(frozen=True, slots=True)
class StrategyEvaluation:
    strategy_id: str
    candidate: StrategyCandidate | None
    metrics: tuple[tuple[str, MetricValue], ...] = ()

    def __post_init__(self) -> None:
        if not self.strategy_id.strip():
            raise ValueError("strategy identity is required")
        if self.candidate is not None and self.candidate.strategy_id != self.strategy_id:
            raise ValueError("candidate strategy identity does not match evaluation")
        names = tuple(name for name, _ in self.metrics)
        if any(not name.strip() for name in names) or len(set(names)) != len(names):
            raise ValueError("metric names must be non-empty and unique")

    def metric(self, name: str) -> MetricValue:
        return dict(self.metrics).get(name)



class Strategy(Protocol):
    strategy_id: str

    def evaluate(self, context: StrategyContext) -> StrategyEvaluation: ...


class MarketDataAdapter(Protocol):
    def snapshot(self) -> StrategyContext: ...

    def final_snapshot(self, initial: StrategyContext) -> StrategyContext: ...


class PreparedExecution(Protocol):
    intent_hash: str
    maximum_all_in_debit: Decimal


class ExecutionAdapter(Protocol):
    def prepare(
        self, candidate: StrategyCandidate, context: StrategyContext
    ) -> PreparedExecution: ...

    def submit(self, prepared: PreparedExecution) -> object: ...


class CapacityBudget(Protocol):
    def reserve(self, prepared: PreparedExecution) -> None: ...
    def release(self, prepared: PreparedExecution) -> None: ...


    def close(self, state: str) -> None: ...


class AuditSink(Protocol):
    def record(self, event: object) -> None: ...
