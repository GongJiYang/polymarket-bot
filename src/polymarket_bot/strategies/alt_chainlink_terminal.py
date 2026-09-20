"""SOL, XRP, and DOGE variants of the verified Chainlink terminal strategy."""

from polymarket_bot.strategies.chainlink_terminal import ChainlinkTerminalStrategy


class SolChainlinkTerminalStrategy(ChainlinkTerminalStrategy):
    strategy_id = "sol_chainlink_terminal_spot_v3"
    asset = "SOL"


class XrpChainlinkTerminalStrategy(ChainlinkTerminalStrategy):
    strategy_id = "xrp_chainlink_terminal_spot_v3"
    asset = "XRP"


class DogeChainlinkTerminalStrategy(ChainlinkTerminalStrategy):
    strategy_id = "doge_chainlink_terminal_spot_v3"
    asset = "DOGE"


__all__ = [
    "DogeChainlinkTerminalStrategy",
    "SolChainlinkTerminalStrategy",
    "XrpChainlinkTerminalStrategy",
]
