"""Typed adapter around the preserved polyrec impulse strategy."""

from __future__ import annotations

from decimal import Decimal

from polymarket_bot.contracts import MetricValue, StrategyContext, StrategyEvaluation
from polymarket_bot.strategies.polyrec_impulse import PolyrecImpulseFadeStrategy


class PolyrecImpulseStrategy:
    strategy_id = PolyrecImpulseFadeStrategy.strategy_id

    def __init__(self) -> None:
        self._strategy = PolyrecImpulseFadeStrategy()

    def evaluate(self, context: StrategyContext) -> StrategyEvaluation:
        raw = self._strategy.evaluate(
            market=context.market,
            up_book=context.up_book,
            down_book=context.down_book,
            current_spot=context.current_spot,
            threshold=context.threshold,
        )
        if raw is None:
            return StrategyEvaluation(
                strategy_id=self.strategy_id,
                candidate=None,
                metrics=(
                    ("signal_active", False),
                    ("execution_eligible", False),
                    ("rejection_code", "SHADOW_ONLY_UNVALIDATED"),
                ),
            )
        try:
            direction = str(raw["direction"])
            signal_metric = str(raw["signal_metric"])
            signal_strength = raw["signal_strength"]
            ask_sum = raw["ask_sum"]
            up_top_ask = raw["up_top_ask"]
            down_top_ask = raw["down_top_ask"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError("polyrec detector returned malformed metrics") from exc
        if direction not in {"up", "down"}:
            raise RuntimeError("polyrec detector returned an invalid direction")
        decimals = (signal_strength, ask_sum, up_top_ask, down_top_ask)
        if any(
            not isinstance(value, Decimal) or not value.is_finite()
            for value in decimals
        ):
            raise RuntimeError("polyrec detector returned non-finite metrics")
        metrics: tuple[tuple[str, MetricValue], ...] = (
            ("signal_active", True),
            ("execution_eligible", False),
            ("rejection_code", "SHADOW_ONLY_UNVALIDATED"),
            ("direction", direction),
            ("signal_metric", signal_metric),
            ("signal_strength", signal_strength),
            ("ask_sum", ask_sum),
            ("up_top_ask", up_top_ask),
            ("down_top_ask", down_top_ask),
        )
        return StrategyEvaluation(
            strategy_id=self.strategy_id,
            candidate=None,
            metrics=metrics,
        )
