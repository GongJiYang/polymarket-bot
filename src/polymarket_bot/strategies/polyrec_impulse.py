"""Shadow-only detector for polyrec-style best-ask impulses.

Source: https://github.com/txbabaxyz/polyrec/blob/main/fade_impulse_backtest.py
License: MIT, Copyright (c) 2026 txBABA.

This module preserves the observable best-ask impulse for research. It does not
emit an executable candidate because a single underdog FAK order is not
economically equivalent to the source strategy's multi-leg portfolio.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

MINIMUM_IMPULSE = Decimal("0.03")
MAXIMUM_ASK_SUM = Decimal("1.02")
SIGNAL_TTL = timedelta(seconds=15)


@dataclass(frozen=True, slots=True)
class _ActiveSignal:
    direction: str
    strength: Decimal
    triggered_at: datetime


class PolyrecImpulseFadeStrategy:
    """Track and report best-ask impulses without proposing an order."""

    strategy_id = "polyrec_impulse_fade_underdog_v1"

    def __init__(self) -> None:
        self._previous: dict[str, tuple[Decimal, Decimal]] = {}
        self._active: dict[str, _ActiveSignal] = {}

    def evaluate(
        self,
        *,
        market: Any,
        up_book: Any,
        down_book: Any,
        current_spot: tuple[datetime, Decimal],
        threshold: Decimal,
    ) -> dict[str, object] | None:
        up_asks = sorted(up_book.asks, key=lambda level: level.price)
        down_asks = sorted(down_book.asks, key=lambda level: level.price)
        if not up_asks or not down_asks:
            self._active.pop(market.market_id, None)
            return None

        up_top = up_asks[0].price
        down_top = down_asks[0].price
        current = (up_top, down_top)
        previous = self._previous.get(market.market_id)
        self._previous[market.market_id] = current
        observed_at = current_spot[0]

        ask_sum = up_top + down_top
        if previous is not None and ask_sum <= MAXIMUM_ASK_SUM:
            strength = max(abs(up_top - previous[0]), abs(down_top - previous[1]))
            if strength >= max(MINIMUM_IMPULSE, threshold):
                direction = "up" if up_top < down_top else "down"
                self._active[market.market_id] = _ActiveSignal(
                    direction=direction,
                    strength=strength,
                    triggered_at=observed_at,
                )

        active = self._active.get(market.market_id)
        if active is None or observed_at - active.triggered_at > SIGNAL_TTL:
            self._active.pop(market.market_id, None)
            return None
        if observed_at < active.triggered_at or ask_sum > MAXIMUM_ASK_SUM:
            return None

        direction = "up" if up_top < down_top else "down"
        if direction != active.direction:
            self._active.pop(market.market_id, None)
            return None

        return {
            "direction": direction,
            "signal_metric": "best_ask_impulse",
            "signal_strength": active.strength,
            "ask_sum": ask_sum,
            "up_top_ask": up_top,
            "down_top_ask": down_top,
        }
