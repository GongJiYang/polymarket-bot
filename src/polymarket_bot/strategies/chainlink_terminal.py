"""Chainlink terminal-TWAP probability edge strategy."""

from __future__ import annotations

from decimal import Decimal

from polymarket_bot.contracts import (
    MetricValue,
    StrategyCandidate,
    StrategyContext,
    StrategyEvaluation,
)
from polymarket_bot.live.bounded_bot import size_buy_debit
from polymarket_bot.live.official_chainlink import terminal_probability
from polymarket_bot.live.transaction_cost import fee_per_share

MINIMUM_TERMINAL_PROBABILITY = Decimal("0.60")
MAXIMUM_ENTRY_PRICE = Decimal("0.75")
MAX_EXECUTION_SLIPPAGE_TICKS = 1
VOLATILITY_STRESS_MULTIPLIER = Decimal("1.25")


class ChainlinkTerminalStrategy:
    strategy_id = "chainlink_terminal_spot_v9"
    asset = "BTC"
    volatility_stress_multiplier = VOLATILITY_STRESS_MULTIPLIER

    def evaluate(self, context: StrategyContext) -> StrategyEvaluation:
        if context.market.asset != self.asset:
            raise ValueError(f"{self.strategy_id} requires {self.asset} market data")
        estimate = terminal_probability(
            context.prices,
            opening=context.opening,
            current_spot=context.current_spot,
            window_end=context.market.window_end,
        )
        stress_multiplier = (
            self.volatility_stress_multiplier
            if self.asset == "BTC"
            else Decimal("1")
        )
        stressed_estimate = (
            terminal_probability(
                context.prices,
                opening=context.opening,
                current_spot=context.current_spot,
                window_end=context.market.window_end,
                volatility_multiplier=stress_multiplier,
            )
            if self.asset == "BTC"
            else estimate
        )
        up_probability = estimate.up_probability
        down_probability = estimate.down_probability
        metrics: list[tuple[str, MetricValue]] = [
            ("up_terminal_probability", up_probability),
            ("down_terminal_probability", down_probability),
            ("up_stressed_terminal_probability", stressed_estimate.up_probability),
            ("down_stressed_terminal_probability", stressed_estimate.down_probability),
            ("volatility_stress_multiplier", stress_multiplier),
            (
                "stressed_volatility_per_sqrt_second",
                stressed_estimate.effective_volatility_per_sqrt_second,
            ),
            (
                "volatility_per_sqrt_second",
                estimate.effective_volatility_per_sqrt_second,
            ),
            (
                "raw_volatility_per_sqrt_second",
                estimate.raw_volatility_per_sqrt_second,
            ),
            ("volatility_floor_active", estimate.volatility_floor_active),
            ("twap_observed_seconds", estimate.twap_observed_seconds),
            ("threshold", context.threshold),
            ("quantity", context.quantity),
            ("target_all_in_debit", context.target_all_in_debit),
            ("minimum_terminal_probability", MINIMUM_TERMINAL_PROBABILITY),
            ("maximum_entry_price", MAXIMUM_ENTRY_PRICE),
            ("maximum_execution_slippage_ticks", MAX_EXECUTION_SLIPPAGE_TICKS),
        ]
        candidates: list[StrategyCandidate] = []
        for direction, token, book, probability, stressed_probability in (
            (
                "up", context.market.up_token_id, context.up_book,
                up_probability, stressed_estimate.up_probability,
            ),
            (
                "down", context.market.down_token_id, context.down_book,
                down_probability, stressed_estimate.down_probability,
            ),
        ):
            model_favored = probability > Decimal("0.5")
            asks = sorted(book.asks, key=lambda level: level.price)
            if not asks:
                metrics.extend(
                    self._direction_metrics(
                        direction,
                        top=None,
                        max_price=None,
                        expected_fill_price=None,
                        fee_share=None,
                        net_edge=None,
                        stressed_net_edge=None,
                        required_shares=None,
                        available=Decimal(0),
                        slippage_ticks=None,
                        execution_gate="book_has_no_asks",
                        eligible=False,
                        model_favored=model_favored,
                    )
                )
                continue
            top = asks[0].price
            if top > MAXIMUM_ENTRY_PRICE:
                fee_share = fee_per_share(
                    top, context.fee.rate, context.fee.exponent
                )
                metrics.extend(
                    self._direction_metrics(
                        direction,
                        top=top,
                        max_price=top,
                        expected_fill_price=top,
                        fee_share=fee_share,
                        net_edge=probability - top - fee_share,
                        stressed_net_edge=stressed_probability - top - fee_share,
                        required_shares=None,
                        available=Decimal(0),
                        slippage_ticks=0,
                        execution_gate="top_ask_above_maximum_entry_price",
                        eligible=False,
                        model_favored=model_favored,
                    )
                )
                continue

            quote = None
            last_limit = top
            last_required = self._required_shares(context, top, book.tick_size)
            last_available = Decimal(0)
            for slippage_ticks in range(MAX_EXECUTION_SLIPPAGE_TICKS + 1):
                limit = top + book.tick_size * slippage_ticks
                if limit > MAXIMUM_ENTRY_PRICE:
                    break
                required = self._required_shares(context, limit, book.tick_size)
                available = sum(
                    (level.size for level in asks if level.price <= limit),
                    Decimal(0),
                )
                last_limit = limit
                last_required = required
                last_available = available
                if (
                    required < context.fee.minimum_size
                    or available < required
                ):
                    continue
                remaining = required
                notional = Decimal(0)
                trading_fee = Decimal(0)
                for level in asks:
                    if level.price > limit or remaining <= 0:
                        break
                    filled = min(level.size, remaining)
                    notional += filled * level.price
                    trading_fee += filled * fee_per_share(
                        level.price, context.fee.rate, context.fee.exponent
                    )
                    remaining -= filled
                if remaining > 0:
                    continue
                quote = (
                    limit,
                    notional / required,
                    trading_fee / required,
                    required,
                    available,
                    slippage_ticks,
                )
                break

            if quote is None:
                metrics.extend(
                    self._direction_metrics(
                        direction,
                        top=top,
                        max_price=last_limit,
                        expected_fill_price=None,
                        fee_share=None,
                        net_edge=None,
                        stressed_net_edge=None,
                        required_shares=last_required,
                        available=last_available,
                        slippage_ticks=None,
                        execution_gate=(
                            "required_shares_below_minimum"
                            if last_required < context.fee.minimum_size
                            else "insufficient_depth_at_maximum_entry_price"
                        ),
                        eligible=False,
                        model_favored=model_favored,
                    )
                )
                continue

            (
                max_price,
                expected_fill_price,
                fee_share,
                required_shares,
                available,
                slippage_ticks,
            ) = quote
            net_edge = probability - expected_fill_price - fee_share
            stressed_net_edge = (
                stressed_probability - expected_fill_price - fee_share
            )
            eligible = (
                probability >= MINIMUM_TERMINAL_PROBABILITY
                and net_edge >= context.threshold
                and stressed_net_edge >= context.threshold
            )
            metrics.extend(
                self._direction_metrics(
                    direction,
                    top=top,
                    max_price=max_price,
                    expected_fill_price=expected_fill_price,
                    fee_share=fee_share,
                    net_edge=net_edge,
                    stressed_net_edge=stressed_net_edge,
                    required_shares=required_shares,
                    available=available,
                    slippage_ticks=slippage_ticks,
                    execution_gate=(
                        "eligible"
                        if eligible
                        else (
                            "terminal_probability_below_minimum"
                            if probability < MINIMUM_TERMINAL_PROBABILITY
                            else (
                                "net_edge_below_threshold"
                                if net_edge < context.threshold
                                else "stressed_net_edge_below_threshold"
                            )
                        )
                    ),
                    eligible=eligible,
                    model_favored=model_favored,
                )
            )
            if not eligible:
                continue
            candidates.append(
                StrategyCandidate(
                    strategy_id=self.strategy_id,
                    direction=direction,
                    token_id=token,
                    book=book,
                    source=context.source,
                    opening=context.opening,
                    prices=context.prices,
                    terminal_probability=probability,
                    volatility_per_sqrt_second=(
                        estimate.effective_volatility_per_sqrt_second
                    ),
                    top_ask=top,
                    max_price=max_price,
                    expected_fill_price=expected_fill_price,
                    fee_per_share=fee_share,
                    net_edge=net_edge,
                    signal_metric="probability_edge",
                    signal_strength=net_edge,
                )
            )
        selected = max(
            candidates,
            key=lambda item: (item.net_edge, item.direction),
            default=None,
        )
        metrics.append(
            ("selected_direction", selected.direction if selected is not None else None)
        )
        return StrategyEvaluation(
            strategy_id=self.strategy_id,
            candidate=selected,
            metrics=tuple(metrics),
        )

    @staticmethod
    def _required_shares(
        context: StrategyContext,
        max_price: Decimal,
        tick_size: Decimal,
    ) -> Decimal:
        if context.target_all_in_debit is None:
            return context.quantity
        return size_buy_debit(
            max_price=max_price,
            tick=tick_size,
            rate=context.fee.rate,
            exponent=context.fee.exponent,
            target_all_in_debit=context.target_all_in_debit,
        ).minimum_fill_shares

    @staticmethod
    def _direction_metrics(
        direction: str,
        *,
        top: MetricValue,
        max_price: MetricValue,
        expected_fill_price: MetricValue,
        fee_share: MetricValue,
        net_edge: MetricValue,
        stressed_net_edge: MetricValue,
        required_shares: MetricValue,
        available: Decimal,
        slippage_ticks: MetricValue,
        execution_gate: str,
        eligible: bool,
        model_favored: bool,
    ) -> tuple[tuple[str, MetricValue], ...]:
        return (
            (f"{direction}_top_ask", top),
            (f"{direction}_max_price", max_price),
            (f"{direction}_expected_fill_price", expected_fill_price),
            (f"{direction}_fee_per_share", fee_share),
            (f"{direction}_net_edge", net_edge),
            (f"{direction}_stressed_net_edge", stressed_net_edge),
            (f"{direction}_required_fill_shares", required_shares),
            (f"{direction}_available_depth", available),
            (f"{direction}_slippage_ticks", slippage_ticks),
            (f"{direction}_execution_gate", execution_gate),
            (f"{direction}_eligible", eligible),
            (f"{direction}_model_favored", model_favored),
        )
