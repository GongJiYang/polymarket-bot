"""ETH-bound Chainlink terminal-price shadow strategy."""

from polymarket_bot.strategies.chainlink_terminal import ChainlinkTerminalStrategy


class EthChainlinkTerminalStrategy(ChainlinkTerminalStrategy):
    """Run shared terminal math only against verified ETH market inputs."""

    strategy_id = "eth_chainlink_terminal_spot_v5"
    asset = "ETH"


__all__ = ["EthChainlinkTerminalStrategy"]
