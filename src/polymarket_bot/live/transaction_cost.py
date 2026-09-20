"""Canonical, auditable execution-cost calculation for Polymarket orders."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Protocol

ZERO = Decimal(0)
ONE = Decimal(1)


class TransactionCostError(ValueError):
    """Raised when a transaction-cost input or result is unsafe."""


class PricedQuantity(Protocol):
    price: Decimal
    size: Decimal


@dataclass(frozen=True, slots=True)
class ExecutionCostLeg:
    """One executable quantity with its market-provided fee metadata."""

    price: Decimal
    quantity: Decimal
    fee_rate: Decimal
    fee_exponent: Decimal

    def __post_init__(self) -> None:
        _probability(self.price, "price")
        _non_negative(self.quantity, "quantity")
        _non_negative(self.fee_rate, "fee_rate")
        _non_negative(self.fee_exponent, "fee_exponent")


@dataclass(frozen=True, slots=True)
class TransactionCostBreakdown:
    """Quote-currency components of one strategy's executable cost bound."""

    notional: Decimal
    trading_fee: Decimal
    explicit_execution_reserve: Decimal
    total_cost: Decimal



def fee_per_share(price: Decimal, rate: Decimal, exponent: Decimal) -> Decimal:
    """Return the official effective fee for one share at ``price``."""

    _probability(price, "price")
    _non_negative(rate, "rate")
    _non_negative(exponent, "exponent")
    try:
        fee = rate * (price * (ONE - price)) ** exponent
    except (ArithmeticError, InvalidOperation, ValueError) as error:
        raise TransactionCostError("fee calculation failed") from error
    if not fee.is_finite() or fee < ZERO:
        raise TransactionCostError("fee calculation is out of range")
    return fee



def calculate_total_cost(
    legs: Sequence[ExecutionCostLeg],
    *,
    explicit_execution_reserve: Decimal = ZERO,
) -> TransactionCostBreakdown:
    """Calculate one all-in cost from immutable legs and an explicit reserve."""

    if not legs:
        raise TransactionCostError("at least one execution leg is required")
    _non_negative(explicit_execution_reserve, "explicit_execution_reserve")

    notional = ZERO
    trading_fee = ZERO
    for leg in legs:
        if type(leg) is not ExecutionCostLeg:
            raise TransactionCostError("execution legs must be ExecutionCostLeg values")
        notional += leg.price * leg.quantity
        trading_fee += leg.quantity * fee_per_share(
            leg.price, leg.fee_rate, leg.fee_exponent
        )

    total_cost = notional + trading_fee + explicit_execution_reserve
    if not all(value.is_finite() and value >= ZERO for value in (
        notional,
        trading_fee,
        total_cost,
    )):
        raise TransactionCostError("transaction cost is out of range")
    return TransactionCostBreakdown(
        notional=notional,
        trading_fee=trading_fee,
        explicit_execution_reserve=explicit_execution_reserve,
        total_cost=total_cost,
    )



def calculate_order_cost(
    order: PricedQuantity,
    rate: Decimal,
    exponent: Decimal,
    *,
    explicit_execution_reserve: Decimal = ZERO,
) -> TransactionCostBreakdown:
    """Calculate the all-in cost of one order-like immutable value."""

    return calculate_total_cost(
        (
            ExecutionCostLeg(
                price=order.price,
                quantity=order.size,
                fee_rate=rate,
                fee_exponent=exponent,
            ),
        ),
        explicit_execution_reserve=explicit_execution_reserve,
    )



def _non_negative(value: Decimal, name: str) -> None:
    if type(value) is not Decimal or not value.is_finite() or value < ZERO:
        raise TransactionCostError(f"{name} must be a finite non-negative Decimal")



def _probability(value: Decimal, name: str) -> None:
    if type(value) is not Decimal or not value.is_finite() or not ZERO < value < ONE:
        raise TransactionCostError(f"{name} must be a Decimal between zero and one")
