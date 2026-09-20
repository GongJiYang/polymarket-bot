"""Lognormal, zero-carry fair probabilities for BTC Up/Down windows.

The model assumes a geometric Brownian motion over the remaining window::

    dS / S = drift * dt + sigma * dW

The public API fixes that time unit to one second: volatility is per square
root second, horizon is in seconds, and drift is per second.  The default drift
is zero, so the price process is a martingale.  The
``-sigma**2 / 2`` term below is intentional: it converts price drift into the
mean of log returns.  This is a baseline model, not a volatility forecast.
"""

from __future__ import annotations

from decimal import Decimal, DecimalException, localcontext


class FairProbabilityError(ValueError):
    """Raised when inputs cannot define a finite lognormal probability."""


_ZERO = Decimal("0")
_ONE = Decimal("1")
_TWO = Decimal("2")
_HALF = Decimal("0.5")
_TAIL_SWITCH = Decimal("1.5")
# Enough digits for the 50-digit working context used by _normal_cdf.
_PI = Decimal("3.141592653589793238462643383279502884197169399375105820974")


def fair_up_down_probabilities(
    *,
    current_price: Decimal,
    window_open_price: Decimal,
    sigma_per_sqrt_second: Decimal,
    tau_seconds: Decimal,
    drift_per_second: Decimal = _ZERO,
) -> tuple[Decimal, Decimal]:
    """Return fair ``(up, down)`` probabilities for a BTC 5m or 15m window.

    The dimensioned argument names are intentional and mandatory.  Convert an
    estimator with another time basis before calling.  Inputs and outputs are
    strictly :class:`~decimal.Decimal`; floats and numeric strings are rejected
    so callers do not silently import binary floating-point error.  The output
    is clipped to the closed unit interval, and ``down`` is computed as
    ``Decimal('1') - up`` to guarantee their sum is *exactly* one.
    """

    current = _positive_decimal(current_price, "current_price")
    opening = _positive_decimal(window_open_price, "window_open_price")
    volatility = _positive_decimal(sigma_per_sqrt_second, "sigma_per_sqrt_second")
    horizon = _positive_decimal(tau_seconds, "tau_seconds")
    carry = _finite_decimal(drift_per_second, "drift_per_second")

    try:
        with localcontext() as context:
            context.prec = 50
            denominator = volatility * horizon.sqrt()
            log_moneyness = (current / opening).ln()
            log_mean = (carry - (volatility * volatility / _TWO)) * horizon
            z_score = (log_moneyness + log_mean) / denominator
            up = _clip_probability(_normal_cdf(z_score))
            return up, _ONE - up
    except FairProbabilityError:
        raise
    except (DecimalException, ArithmeticError, ValueError) as error:
        raise FairProbabilityError(
            "inputs caused invalid Decimal arithmetic"
        ) from error


def fair_terminal_twap_probabilities(
    *,
    current_price: Decimal,
    window_open_price: Decimal,
    sigma_per_sqrt_second: Decimal,
    seconds_to_resolution: Decimal,
    twap_lookback_seconds: Decimal = Decimal("60"),
    drift_per_second: Decimal = _ZERO,
) -> tuple[Decimal, Decimal]:
    """Approximate the terminal-TWAP outcome probability.

    The resolution value is an arithmetic TWAP over the final ``lookback``
    seconds, not the latest already-lagged TWAP.  Under the local Brownian
    approximation, that future average has the same variance as a point price
    at ``seconds_to_resolution - 2 * lookback / 3``.  The final TWAP window
    must still be entirely in the future; otherwise its already-fixed path is
    required and this baseline model fails closed.
    """

    horizon = _positive_decimal(seconds_to_resolution, "seconds_to_resolution")
    lookback = _positive_decimal(twap_lookback_seconds, "twap_lookback_seconds")
    if horizon < lookback:
        raise FairProbabilityError(
            "terminal TWAP window has begun; observed path is required"
        )
    effective_horizon = horizon - (_TWO * lookback / Decimal("3"))
    return fair_up_down_probabilities(
        current_price=current_price,
        window_open_price=window_open_price,
        sigma_per_sqrt_second=sigma_per_sqrt_second,
        tau_seconds=effective_horizon,
        drift_per_second=drift_per_second,
    )


def fair_in_progress_terminal_twap_probabilities(
    *,
    current_price: Decimal,
    window_open_price: Decimal,
    sigma_per_sqrt_second: Decimal,
    observed_price_seconds: Decimal,
    observed_seconds: Decimal,
    twap_lookback_seconds: Decimal = Decimal("60"),
    drift_per_second: Decimal = _ZERO,
) -> tuple[Decimal, Decimal]:
    """Approximate a terminal TWAP after part of its path is observed.

    ``observed_price_seconds`` is the time integral of the observed price path
    since the final TWAP window began.  The known path changes the threshold
    that the average over the remaining interval must exceed.  Under the same
    local Brownian approximation used by
    :func:`fair_terminal_twap_probabilities`, that future average has the
    variance of a point price one third of the remaining interval ahead.
    """

    current = _positive_decimal(current_price, "current_price")
    opening = _positive_decimal(window_open_price, "window_open_price")
    volatility = _positive_decimal(
        sigma_per_sqrt_second, "sigma_per_sqrt_second"
    )
    lookback = _positive_decimal(
        twap_lookback_seconds, "twap_lookback_seconds"
    )
    elapsed = _finite_decimal(observed_seconds, "observed_seconds")
    integral = _finite_decimal(
        observed_price_seconds, "observed_price_seconds"
    )
    carry = _finite_decimal(drift_per_second, "drift_per_second")
    if elapsed < _ZERO or elapsed >= lookback:
        raise FairProbabilityError(
            "observed_seconds must be non-negative and less than TWAP lookback"
        )
    if integral < _ZERO:
        raise FairProbabilityError("observed_price_seconds must not be negative")
    if elapsed == _ZERO and integral != _ZERO:
        raise FairProbabilityError(
            "observed_price_seconds must be zero when observed_seconds is zero"
        )

    remaining = lookback - elapsed
    required_integral = opening * lookback - integral
    if required_integral <= _ZERO:
        return _ONE, _ZERO
    required_future_average = required_integral / remaining
    return fair_up_down_probabilities(
        current_price=current,
        window_open_price=required_future_average,
        sigma_per_sqrt_second=volatility,
        tau_seconds=remaining / Decimal("3"),
        drift_per_second=carry,
    )


def _normal_cdf(value: Decimal) -> Decimal:
    """Evaluate the standard-normal CDF with stable center and tail methods."""

    if value < -_TAIL_SWITCH:
        return _normal_survival(-value)
    if value < _ZERO:
        return _ONE - _normal_cdf(-value)
    if value > _TAIL_SWITCH:
        return _ONE - _normal_survival(value)

    with localcontext() as context:
        context.prec = 50
        x = value / _TWO.sqrt()
        x_squared = x * x
        term = x
        series = term
        cutoff = Decimal(1).scaleb(-(context.prec + 8))
        for index in range(1, 10_000):
            decimal_index = Decimal(index)
            term *= (
                -x_squared
                * Decimal(2 * index - 1)
                / (decimal_index * Decimal(2 * index + 1))
            )
            series += term
            if abs(term) <= cutoff:
                break
        else:  # pragma: no cover - the center series converges rapidly
            raise FairProbabilityError("normal CDF did not converge")
        erf = _TWO * series / _PI.sqrt()
        return _HALF * (_ONE + erf)


def _normal_survival(value: Decimal) -> Decimal:
    """Return ``P[N(0, 1) > value]`` via a continued incomplete-gamma fraction.

    Unlike the erf power series, this evaluates the small tail directly and
    does not subtract large alternating terms.  ``value`` is positive and
    greater than ``_TAIL_SWITCH`` at every call site.
    """

    with localcontext() as context:
        context.prec = 60
        gamma_argument = value * value / _TWO
        b = gamma_argument + _HALF
        tiny = Decimal(1).scaleb(-(context.prec * 2))
        c = _ONE / tiny
        d = _ONE / b
        fraction = d
        cutoff = Decimal(1).scaleb(-(context.prec - 8))
        for index in range(1, 10_000):
            index_decimal = Decimal(index)
            coefficient = -index_decimal * (index_decimal - _HALF)
            b += _TWO
            d = coefficient * d + b
            if abs(d) < tiny:
                d = tiny
            c = b + coefficient / c
            if abs(c) < tiny:
                c = tiny
            d = _ONE / d
            delta = d * c
            fraction *= delta
            if abs(delta - _ONE) <= cutoff:
                break
        else:  # pragma: no cover - defensive convergence bound
            raise FairProbabilityError("normal tail did not converge")

        regularized_gamma = (
            (-gamma_argument + _HALF * gamma_argument.ln()).exp()
            * fraction
            / _PI.sqrt()
        )
        return _HALF * regularized_gamma


def _clip_probability(value: Decimal) -> Decimal:
    """Clip finite numerical round-off at the probability boundaries."""

    if value <= _ZERO:
        return _ZERO
    if value >= _ONE:
        return _ONE
    return value


def _positive_decimal(value: Decimal, name: str) -> Decimal:
    result = _finite_decimal(value, name)
    if result <= _ZERO:
        raise FairProbabilityError(f"{name} must be positive")
    return result


def _finite_decimal(value: Decimal, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise FairProbabilityError(f"{name} must be finite")
    return value
