"""Built-in and installed strategy discovery."""

from __future__ import annotations

from importlib import metadata
from typing import Iterable

from polymarket_bot.contracts import Strategy
from polymarket_bot.strategies.alt_chainlink_terminal import (
    DogeChainlinkTerminalStrategy,
    SolChainlinkTerminalStrategy,
    XrpChainlinkTerminalStrategy,
)
from polymarket_bot.strategies.chainlink_terminal import ChainlinkTerminalStrategy
from polymarket_bot.strategies.eth_chainlink_terminal import (
    EthChainlinkTerminalStrategy,
)
from polymarket_bot.strategies.polyrec_adapter import PolyrecImpulseStrategy

ENTRY_POINT_GROUP = "polymarket_bot.strategies"
ALL_STRATEGIES = "all"
DEFAULT_LIVE_STRATEGY = ChainlinkTerminalStrategy.strategy_id
LIVE_STRATEGY_IDS = frozenset(
    {
        DEFAULT_LIVE_STRATEGY,
        EthChainlinkTerminalStrategy.strategy_id,
        SolChainlinkTerminalStrategy.strategy_id,
        XrpChainlinkTerminalStrategy.strategy_id,
        DogeChainlinkTerminalStrategy.strategy_id,
    }
)


class StrategyRegistryError(RuntimeError):
    pass


def load_strategies(
    *, entry_points: Iterable[object] | None = None
) -> dict[str, Strategy]:
    builtins: tuple[Strategy, ...] = (
        ChainlinkTerminalStrategy(),
        EthChainlinkTerminalStrategy(),
        SolChainlinkTerminalStrategy(),
        XrpChainlinkTerminalStrategy(),
        DogeChainlinkTerminalStrategy(),
        PolyrecImpulseStrategy(),
    )
    registry = {strategy.strategy_id: strategy for strategy in builtins}
    discovered = entry_points
    if discovered is None:
        discovered = metadata.entry_points(group=ENTRY_POINT_GROUP)
    for point in discovered:
        name = getattr(point, "name", "")
        if not isinstance(name, str) or not name:
            raise StrategyRegistryError("strategy entry point has no name")
        loaded = point.load()  # type: ignore[attr-defined]
        strategy = loaded() if isinstance(loaded, type) else loaded
        strategy_id = getattr(strategy, "strategy_id", None)
        evaluate = getattr(strategy, "evaluate", None)
        if strategy_id != name or not callable(evaluate):
            raise StrategyRegistryError(
                f"strategy entry point {name!r} violates the plugin contract"
            )
        if name in registry:
            if type(registry[name]) is type(strategy):
                continue
            raise StrategyRegistryError(f"duplicate strategy id: {name}")
        registry[name] = strategy
    return dict(sorted(registry.items()))


def select_strategies(
    registry: dict[str, Strategy], selection: str
) -> tuple[Strategy, ...]:
    if selection == ALL_STRATEGIES:
        return tuple(registry.values())
    try:
        return (registry[selection],)
    except KeyError as exc:
        raise StrategyRegistryError(f"unknown strategy: {selection}") from exc


def select_live_strategies(
    registry: dict[str, Strategy], selection: str
) -> tuple[Strategy, ...]:
    """Select only strategies explicitly approved for real-money execution."""
    if selection == ALL_STRATEGIES:
        selected_ids = tuple(sorted(LIVE_STRATEGY_IDS.intersection(registry)))
        if not selected_ids:
            raise StrategyRegistryError("no approved live strategy is available")
        return tuple(registry[strategy_id] for strategy_id in selected_ids)
    if selection not in LIVE_STRATEGY_IDS:
        raise StrategyRegistryError(
            f"strategy is not approved for live execution: {selection}"
        )
    return select_strategies(registry, selection)
