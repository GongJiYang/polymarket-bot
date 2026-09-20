from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from polymarket_bot.contracts import FeeMetadata, StrategyContext
from polymarket_bot.microstructure.models import (
    BookLevel,
    MarketInterval,
    MarketMetadata,
    OrderBook,
    OutcomeSide,
)
from polymarket_bot.live.official_chainlink import TerminalProbabilityEstimate
from polymarket_bot.strategies.alt_chainlink_terminal import (
    DogeChainlinkTerminalStrategy,
    SolChainlinkTerminalStrategy,
    XrpChainlinkTerminalStrategy,
)
from polymarket_bot.strategies.chainlink_terminal import ChainlinkTerminalStrategy
from polymarket_bot.strategies.eth_chainlink_terminal import (
    EthChainlinkTerminalStrategy,
)

NOW = datetime(2026, 8, 25, 12, 1, tzinfo=timezone.utc)


def terminal_estimate(
    up: Decimal,
    down: Decimal,
    sigma: Decimal,
) -> TerminalProbabilityEstimate:
    return TerminalProbabilityEstimate(
        up_probability=up,
        down_probability=down,
        raw_volatility_per_sqrt_second=sigma,
        effective_volatility_per_sqrt_second=sigma,
        volatility_floor_active=False,
        twap_observed_seconds=Decimal("0"),
    )


def strategy_context(
    *,
    up_ask: str = "0.45",
    down_ask: str = "0.55",
    up_asks: tuple[tuple[str, str], ...] | None = None,
    down_asks: tuple[tuple[str, str], ...] | None = None,
    target_all_in_debit: Decimal | None = None,
) -> StrategyContext:
    start = NOW.replace(minute=0, second=0, microsecond=0)
    market = MarketMetadata(
        market_id="condition",
        title="Bitcoin Up or Down",
        rules="Chainlink",
        asset="BTC",
        interval=MarketInterval.FIVE_MINUTES,
        window_start=start,
        window_end=start + timedelta(minutes=5),
        up_token_id="up",
        down_token_id="down",
        outcomes=("Up", "Down"),
    )

    def book(
        token: str,
        outcome: OutcomeSide,
        ask: str,
        levels: tuple[tuple[str, str], ...] | None,
    ) -> OrderBook:
        ask_levels = ((ask, "20"),) if levels is None else levels
        return OrderBook(
            market_id="condition",
            token_id=token,
            outcome=outcome,
            sequence=1,
            bids=(
                BookLevel(
                    price=(
                        Decimal(ask_levels[0][0]) - Decimal("0.01")
                        if ask_levels
                        else Decimal("0.01")
                    ),
                    size=Decimal("20"),
                ),
            ),
            asks=tuple(
                BookLevel(price=Decimal(price), size=Decimal(size))
                for price, size in ask_levels
            ),
            tick_size=Decimal("0.01"),
            exchange_at=NOW,
            received_at=NOW,
            tradable=bool(ask_levels),
            reason=None if ask_levels else "no asks",
        )

    return StrategyContext(
        market=market,
        up_book=book("up", OutcomeSide.UP, up_ask, up_asks),
        down_book=book("down", OutcomeSide.DOWN, down_ask, down_asks),
        fee=FeeMetadata(Decimal("0"), Decimal("0"), Decimal("5")),
        source="chainlink",
        opening=Decimal("70000"),
        prices=((NOW, Decimal("70000")),),
        current_spot=(NOW, Decimal("70000")),
        threshold=Decimal("0.04"),
        quantity=Decimal("5"),
        target_all_in_debit=target_all_in_debit,
    )


def test_terminal_strategy_preserves_probability_edge_rule(monkeypatch) -> None:
    monkeypatch.setattr(
        "polymarket_bot.strategies.chainlink_terminal.terminal_probability",
        lambda *_args, **_kwargs: terminal_estimate(Decimal("0.60"), Decimal("0.30"), Decimal("0.001")),
    )
    evaluation = ChainlinkTerminalStrategy().evaluate(strategy_context())
    candidate = evaluation.candidate
    assert candidate is not None
    assert candidate.direction == "up"
    assert candidate.max_price == Decimal("0.45")
    assert candidate.expected_fill_price == Decimal("0.45")
    assert candidate.net_edge == Decimal("0.15")
    assert evaluation.metric("up_terminal_probability") == Decimal("0.60")
    assert evaluation.metric("up_top_ask") == Decimal("0.45")
    assert evaluation.metric("up_available_depth") == Decimal("20")
    assert evaluation.metric("up_required_fill_shares") == Decimal("5")
    assert evaluation.metric("up_slippage_ticks") == 0
    assert evaluation.metric("up_eligible") is True
    assert evaluation.metric("up_model_favored") is True
    assert evaluation.metric("selected_direction") == "up"


def test_terminal_strategy_keeps_top_ask_with_sufficient_depth(monkeypatch) -> None:
    monkeypatch.setattr(
        "polymarket_bot.strategies.chainlink_terminal.terminal_probability",
        lambda *_args, **_kwargs: terminal_estimate(Decimal("0.2626031498184398355"), Decimal("0.7373968501815601645"), Decimal("0.001")),
    )

    evaluation = ChainlinkTerminalStrategy().evaluate(
        strategy_context(up_ask="0.66", down_ask="0.34")
    )

    candidate = evaluation.candidate
    assert candidate is not None
    assert candidate.direction == "down"
    assert candidate.top_ask == Decimal("0.34")
    assert candidate.max_price == Decimal("0.34")
    assert candidate.expected_fill_price == Decimal("0.34")
    assert candidate.net_edge == Decimal("0.3973968501815601645")


def test_terminal_strategy_uses_executable_price_for_edge(monkeypatch) -> None:
    monkeypatch.setattr(
        "polymarket_bot.strategies.chainlink_terminal.terminal_probability",
        lambda *_args, **_kwargs: terminal_estimate(Decimal("0.70"), Decimal("0.30"), Decimal("0.001")),
    )

    evaluation = ChainlinkTerminalStrategy().evaluate(strategy_context(up_ask="0.64"))

    candidate = evaluation.candidate
    assert candidate is not None
    assert candidate.max_price == Decimal("0.64")
    assert candidate.expected_fill_price == Decimal("0.64")
    assert candidate.net_edge == Decimal("0.06")


@pytest.mark.parametrize("direction", ["up", "down"])
@pytest.mark.parametrize("ask, eligible", [("0.60", False), ("0.55", True)])
def test_terminal_strategy_stress_rejects_fragile_and_keeps_robust_trade(
    direction: str, ask: str, eligible: bool
) -> None:
    context = strategy_context(
        up_ask=ask if direction == "up" else "0.90",
        down_ask=ask if direction == "down" else "0.90",
    )
    spot = Decimal("70020") if direction == "up" else Decimal("69980")
    context = replace(
        context,
        prices=tuple(
            (NOW - timedelta(seconds=180 - offset), spot + Decimal(offset % 2))
            for offset in range(181)
        ),
        current_spot=(NOW, spot),
    )
    evaluation = ChainlinkTerminalStrategy().evaluate(context)

    assert evaluation.metric(f"{direction}_net_edge") >= context.threshold
    assert evaluation.metric(f"{direction}_terminal_probability") >= Decimal("0.60")
    assert (
        evaluation.metric(f"{direction}_stressed_terminal_probability")
        < evaluation.metric(f"{direction}_terminal_probability")
    )
    assert evaluation.metric("volatility_stress_multiplier") == Decimal("1.25")
    assert evaluation.metric("stressed_volatility_per_sqrt_second") == Decimal("0.0000625")
    assert evaluation.metric(f"{direction}_eligible") is eligible
    if eligible:
        assert evaluation.metric(f"{direction}_stressed_net_edge") >= context.threshold
        assert evaluation.candidate is not None
        assert evaluation.candidate.direction == direction
    else:
        assert evaluation.metric(f"{direction}_stressed_net_edge") < context.threshold
        assert evaluation.metric(f"{direction}_execution_gate") == "stressed_net_edge_below_threshold"
        assert evaluation.candidate is None


@pytest.mark.parametrize(
    "stressed_probability, eligible",
    [(Decimal("0.629999"), False), (Decimal("0.63"), True)],
)
def test_terminal_strategy_stressed_edge_includes_fees_at_threshold(
    monkeypatch, stressed_probability: Decimal, eligible: bool
) -> None:
    def estimate(*_args, volatility_multiplier=Decimal("1"), **_kwargs):
        up = Decimal("0.70") if volatility_multiplier == 1 else stressed_probability
        return terminal_estimate(up, Decimal("1") - up, Decimal("0.001"))

    monkeypatch.setattr(
        "polymarket_bot.strategies.chainlink_terminal.terminal_probability",
        estimate,
    )
    context = replace(
        strategy_context(up_ask="0.58", down_ask="0.90"),
        fee=FeeMetadata(Decimal("0.01"), Decimal("0"), Decimal("5")),
    )
    evaluation = ChainlinkTerminalStrategy().evaluate(context)

    assert evaluation.metric("up_net_edge") == Decimal("0.11")
    assert evaluation.metric("up_stressed_net_edge") == stressed_probability - Decimal("0.59")
    assert evaluation.metric("up_eligible") is eligible
    assert (evaluation.candidate is not None) is eligible
    assert evaluation.metric("up_execution_gate") == (
        "eligible" if eligible else "stressed_net_edge_below_threshold"
    )


@pytest.mark.parametrize(
    "strategy_type, asset",
    [
        (EthChainlinkTerminalStrategy, "ETH"),
        (SolChainlinkTerminalStrategy, "SOL"),
        (XrpChainlinkTerminalStrategy, "XRP"),
        (DogeChainlinkTerminalStrategy, "DOGE"),
    ],
)
def test_terminal_stress_gate_preserves_other_assets(strategy_type, asset: str) -> None:
    context = strategy_context(up_ask="0.60", down_ask="0.90")
    context = replace(
        context,
        market=context.market.model_copy(update={"asset": asset}),
        prices=tuple(
            (
                NOW - timedelta(seconds=180 - offset),
                Decimal("70020") + Decimal(offset % 2),
            )
            for offset in range(181)
        ),
        current_spot=(NOW, Decimal("70020")),
    )
    evaluation = strategy_type().evaluate(context)

    assert evaluation.candidate is not None
    assert evaluation.candidate.direction == "up"
    assert evaluation.metric("volatility_stress_multiplier") == Decimal("1")
    assert evaluation.metric("up_stressed_net_edge") == evaluation.metric("up_net_edge")


def test_terminal_strategy_rejects_positive_edge_on_model_underdog(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "polymarket_bot.strategies.chainlink_terminal.terminal_probability",
        lambda *_args, **_kwargs: terminal_estimate(Decimal("0.43"), Decimal("0.57"), Decimal("0.001")),
    )

    evaluation = ChainlinkTerminalStrategy().evaluate(
        strategy_context(up_ask="0.20", down_ask="0.90")
    )

    assert evaluation.metric("up_net_edge") == Decimal("0.23")
    assert evaluation.metric("up_model_favored") is False
    assert evaluation.metric("up_eligible") is False
    assert evaluation.candidate is None
    assert evaluation.metric("selected_direction") is None


def test_terminal_strategy_requires_probability_floor() -> None:
    from unittest.mock import patch

    with patch(
        "polymarket_bot.strategies.chainlink_terminal.terminal_probability",
        return_value=terminal_estimate(
            Decimal("0.599"), Decimal("0.401"), Decimal("0.001")
        ),
    ):
        evaluation = ChainlinkTerminalStrategy().evaluate(
            strategy_context(up_ask="0.20")
        )

    assert evaluation.metric("up_net_edge") == Decimal("0.399")
    assert evaluation.metric("up_eligible") is False
    assert evaluation.metric("up_execution_gate") == "terminal_probability_below_minimum"
    assert evaluation.metric("minimum_terminal_probability") == Decimal("0.60")
    assert evaluation.candidate is None


def test_terminal_strategy_rejects_missing_ask_despite_favorable_model(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "polymarket_bot.strategies.chainlink_terminal.terminal_probability",
        lambda *_args, **_kwargs: terminal_estimate(Decimal("0.70"), Decimal("0.30"), Decimal("0.001")),
    )

    evaluation = ChainlinkTerminalStrategy().evaluate(
        strategy_context(up_asks=(), down_ask="0.20")
    )

    assert evaluation.metric("up_expected_fill_price") is None
    assert evaluation.metric("up_execution_gate") == "book_has_no_asks"
    assert evaluation.metric("up_eligible") is False
    assert evaluation.candidate is None


def test_terminal_strategy_rejects_entry_price_above_cap() -> None:
    from unittest.mock import patch

    with patch(
        "polymarket_bot.strategies.chainlink_terminal.terminal_probability",
        return_value=terminal_estimate(
            Decimal("0.95"), Decimal("0.05"), Decimal("0.001")
        ),
    ):
        evaluation = ChainlinkTerminalStrategy().evaluate(
            strategy_context(up_ask="0.76")
        )

    assert evaluation.metric("up_max_price") == Decimal("0.76")
    assert evaluation.metric("up_execution_gate") == "top_ask_above_maximum_entry_price"
    assert evaluation.metric("up_eligible") is False
    assert evaluation.candidate is None


def test_terminal_strategy_keeps_executable_top_ask_when_next_tick_loses_edge(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "polymarket_bot.strategies.chainlink_terminal.terminal_probability",
        lambda *_args, **_kwargs: terminal_estimate(Decimal("0.70"), Decimal("0.30"), Decimal("0.001")),
    )

    evaluation = ChainlinkTerminalStrategy().evaluate(
        strategy_context(up_ask="0.66")
    )

    candidate = evaluation.candidate
    assert candidate is not None
    assert candidate.max_price == Decimal("0.66")
    assert candidate.net_edge == Decimal("0.04")


def test_terminal_strategy_uses_one_tick_when_top_depth_is_insufficient(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "polymarket_bot.strategies.chainlink_terminal.terminal_probability",
        lambda *_args, **_kwargs: terminal_estimate(Decimal("0.70"), Decimal("0.30"), Decimal("0.001")),
    )

    evaluation = ChainlinkTerminalStrategy().evaluate(
        strategy_context(up_asks=(("0.45", "2"), ("0.46", "20")))
    )

    candidate = evaluation.candidate
    assert candidate is not None
    assert candidate.max_price == Decimal("0.46")
    assert candidate.expected_fill_price == Decimal("0.456")
    assert candidate.net_edge == Decimal("0.244")
    assert evaluation.metric("up_required_fill_shares") == Decimal("5")
    assert evaluation.metric("up_available_depth") == Decimal("22")
    assert evaluation.metric("up_slippage_ticks") == 1


def test_terminal_strategy_rejects_depth_beyond_one_tick(monkeypatch) -> None:
    monkeypatch.setattr(
        "polymarket_bot.strategies.chainlink_terminal.terminal_probability",
        lambda *_args, **_kwargs: terminal_estimate(Decimal("0.70"), Decimal("0.30"), Decimal("0.001")),
    )

    evaluation = ChainlinkTerminalStrategy().evaluate(
        strategy_context(
            up_asks=(("0.45", "2"), ("0.46", "2"), ("0.47", "20"))
        )
    )

    assert evaluation.candidate is None
    assert evaluation.metric("up_max_price") == Decimal("0.46")
    assert evaluation.metric("up_expected_fill_price") is None
    assert evaluation.metric("up_required_fill_shares") == Decimal("5")
    assert evaluation.metric("up_available_depth") == Decimal("4")
    assert evaluation.metric("up_eligible") is False


def test_terminal_strategy_sizes_depth_for_four_dollar_all_in_cap(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "polymarket_bot.strategies.chainlink_terminal.terminal_probability",
        lambda *_args, **_kwargs: terminal_estimate(Decimal("0.70"), Decimal("0.30"), Decimal("0.001")),
    )

    evaluation = ChainlinkTerminalStrategy().evaluate(
        strategy_context(
            up_asks=(("0.45", "8"), ("0.46", "20")),
            target_all_in_debit=Decimal("4"),
        )
    )

    candidate = evaluation.candidate
    assert candidate is not None
    assert candidate.max_price == Decimal("0.46")
    assert candidate.expected_fill_price > Decimal("0.45")
    assert candidate.expected_fill_price <= candidate.max_price
    assert evaluation.metric("up_required_fill_shares") == Decimal("8.695652")
    assert evaluation.metric("up_slippage_ticks") == 1


def test_terminal_strategy_rejects_extreme_ask_without_invalid_fee_price() -> None:
    from unittest.mock import patch

    with patch(
        "polymarket_bot.strategies.chainlink_terminal.terminal_probability",
        return_value=terminal_estimate(
            Decimal("0.95"), Decimal("0.05"), Decimal("0.001")
        ),
    ):
        evaluation = ChainlinkTerminalStrategy().evaluate(
            strategy_context(up_ask="0.99")
        )

    assert evaluation.metric("up_max_price") == Decimal("0.99")
    assert evaluation.metric("up_eligible") is False
    assert evaluation.candidate is None


def test_eth_strategy_has_distinct_identity_and_rejects_btc_context(
    monkeypatch,
) -> None:
    strategy = EthChainlinkTerminalStrategy()
    with pytest.raises(ValueError, match="requires ETH market data"):
        strategy.evaluate(strategy_context())

    monkeypatch.setattr(
        "polymarket_bot.strategies.chainlink_terminal.terminal_probability",
        lambda *_args, **_kwargs: terminal_estimate(Decimal("0.70"), Decimal("0.30"), Decimal("0.001")),
    )
    btc_context = strategy_context()
    eth_context = replace(
        btc_context,
        market=btc_context.market.model_copy(
            update={
                "title": "Ethereum Up or Down",
                "asset": "ETH",
            }
        ),
        source="https://data.chain.link/streams/eth-usd",
    )

    evaluation = strategy.evaluate(eth_context)

    assert evaluation.strategy_id == "eth_chainlink_terminal_spot_v5"
    assert evaluation.candidate is not None
    assert evaluation.candidate.strategy_id == evaluation.strategy_id


@pytest.mark.parametrize(
    ("strategy_type", "asset", "strategy_id"),
    (
        (SolChainlinkTerminalStrategy, "SOL", "sol_chainlink_terminal_spot_v3"),
        (XrpChainlinkTerminalStrategy, "XRP", "xrp_chainlink_terminal_spot_v3"),
        (DogeChainlinkTerminalStrategy, "DOGE", "doge_chainlink_terminal_spot_v3"),
    ),
)
def test_alt_strategy_is_asset_bound_and_has_distinct_identity(
    strategy_type, asset: str, strategy_id: str
) -> None:
    strategy = strategy_type()
    with pytest.raises(ValueError, match=f"requires {asset} market data"):
        strategy.evaluate(strategy_context())

    assert strategy.asset == asset
    assert strategy.strategy_id == strategy_id
