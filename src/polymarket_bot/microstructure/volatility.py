"""Fail-closed short-horizon volatility estimators.

The estimators operate only on explicit, UTC timestamps and :class:`Decimal`
prices.  They deliberately return ``None`` instead of inventing a volatility
when their input cannot support one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, DecimalException


@dataclass(frozen=True, slots=True)
class PriceObservation:
    """One positive price observed at an explicit UTC time."""

    price: Decimal
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class VolatilityEstimate:
    """A log-return volatility rate whose time unit is explicitly one second."""

    method: str
    volatility_per_sqrt_second: Decimal
    as_of: datetime
    window_start: datetime
    return_count: int


def rolling_realized_volatility(
    observations: Sequence[PriceObservation],
    *,
    as_of: datetime,
    window: timedelta,
    minimum_returns: int,
    maximum_gap: timedelta,
) -> VolatilityEstimate | None:
    """Return root-mean-square log-return volatility, or ``None`` if unusable.

    Observations after ``as_of`` are ignored so a caller cannot accidentally
    use information from after its stated decision time.  A gap longer than
    ``maximum_gap``, duplicate/out-of-order time, invalid price, or too few
    returns makes the entire estimate unavailable.
    """

    prepared = _prepare(
        observations,
        as_of=as_of,
        window=window,
        minimum_returns=minimum_returns,
        maximum_gap=maximum_gap,
    )
    if prepared is None:
        return None
    points, window_start = prepared
    returns = _log_returns(points)
    if returns is None:
        return None

    try:
        elapsed_seconds = sum(_interval_seconds(points), Decimal("0"))
        variance_per_second = (
            sum((value * value for value in returns), Decimal("0")) / elapsed_seconds
        )
    except (DecimalException, ValueError, OverflowError):
        return None
    volatility = _positive_finite_sqrt(variance_per_second)
    if volatility is None:
        return None
    return VolatilityEstimate(
        method="rolling_realized",
        volatility_per_sqrt_second=volatility,
        as_of=as_of,
        window_start=window_start,
        return_count=len(returns),
    )


def ewma_volatility(
    observations: Sequence[PriceObservation],
    *,
    as_of: datetime,
    window: timedelta,
    minimum_returns: int,
    maximum_gap: timedelta,
    decay: Decimal,
) -> VolatilityEstimate | None:
    """Return a fail-closed EWMA log-return volatility estimate.

    ``decay`` is the previous-variance weight and must be a finite Decimal
    strictly between zero and one.  Squared returns are divided by their exact
    elapsed seconds before applying the EWMA recursion, so the returned value
    is always per square root second even with irregular sampling.
    """

    if (
        not _is_decimal(decay)
        or not decay.is_finite()
        or not Decimal("0") < decay < Decimal("1")
    ):
        return None
    prepared = _prepare(
        observations,
        as_of=as_of,
        window=window,
        minimum_returns=minimum_returns,
        maximum_gap=maximum_gap,
    )
    if prepared is None:
        return None
    points, window_start = prepared
    returns = _log_returns(points)
    if returns is None:
        return None

    try:
        intervals = _interval_seconds(points)
        variance_per_second = returns[0] * returns[0] / intervals[0]
        for value, interval in zip(returns[1:], intervals[1:]):
            squared_return_rate = value * value / interval
            variance_per_second = (decay * variance_per_second) + (
                (Decimal("1") - decay) * squared_return_rate
            )
    except (DecimalException, ValueError, OverflowError):
        return None
    volatility = _positive_finite_sqrt(variance_per_second)
    if volatility is None:
        return None
    return VolatilityEstimate(
        method="ewma",
        volatility_per_sqrt_second=volatility,
        as_of=as_of,
        window_start=window_start,
        return_count=len(returns),
    )


def _prepare(
    observations: Sequence[PriceObservation],
    *,
    as_of: datetime,
    window: timedelta,
    minimum_returns: int,
    maximum_gap: timedelta,
) -> tuple[list[PriceObservation], datetime] | None:
    if not _is_utc_datetime(as_of):
        return None
    if not isinstance(window, timedelta) or window <= timedelta(0):
        return None
    if not isinstance(maximum_gap, timedelta) or maximum_gap <= timedelta(0):
        return None
    if (
        isinstance(minimum_returns, bool)
        or not isinstance(minimum_returns, int)
        or minimum_returns < 1
    ):
        return None

    try:
        window_start = as_of - window
    except OverflowError:
        return None
    points: list[PriceObservation] = []
    for point in observations:
        if not isinstance(point, PriceObservation) or not _is_utc_datetime(
            point.observed_at
        ):
            return None
        if window_start <= point.observed_at <= as_of:
            if not _valid_point(point):
                return None
            points.append(point)
    if len(points) < minimum_returns + 1:
        return None

    previous: PriceObservation | None = None
    for point in points:
        if previous is not None:
            gap = point.observed_at - previous.observed_at
            if gap <= timedelta(0) or gap > maximum_gap:
                return None
        previous = point
    return points, window_start


def _valid_point(point: object) -> bool:
    return (
        isinstance(point, PriceObservation)
        and _is_decimal(point.price)
        and point.price.is_finite()
        and point.price > 0
        and _is_utc_datetime(point.observed_at)
    )


def _log_returns(points: Sequence[PriceObservation]) -> list[Decimal] | None:
    values: list[Decimal] = []
    try:
        for previous, current in zip(points, points[1:]):
            value = (current.price / previous.price).ln()
            if not value.is_finite():
                return None
            values.append(value)
    except (DecimalException, ValueError, OverflowError):
        return None
    return values


def _interval_seconds(points: Sequence[PriceObservation]) -> list[Decimal]:
    """Return exact positive intervals, expressed in Decimal seconds."""

    intervals: list[Decimal] = []
    for previous, current in zip(points, points[1:]):
        delta = current.observed_at - previous.observed_at
        seconds = Decimal(delta.days * 86_400 + delta.seconds) + (
            Decimal(delta.microseconds) / Decimal("1000000")
        )
        if not seconds.is_finite() or seconds <= 0:
            raise ValueError("return interval must be positive and finite")
        intervals.append(seconds)
    return intervals


def _positive_finite_sqrt(variance: Decimal) -> Decimal | None:
    if not variance.is_finite() or variance <= 0:
        return None
    try:
        result = variance.sqrt()
    except (DecimalException, ValueError, OverflowError):
        return None
    return result if result.is_finite() and result > 0 else None


def _is_decimal(value: object) -> bool:
    return isinstance(value, Decimal) and not isinstance(value, bool)


def _is_utc_datetime(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
    )
