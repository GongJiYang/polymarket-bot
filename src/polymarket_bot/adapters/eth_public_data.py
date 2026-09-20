"""GET-only ETH Up/Down snapshot adapter."""

from polymarket_bot.adapters.public_data import CurrentBtcSnapshotAdapter


class CurrentEthSnapshotAdapter(CurrentBtcSnapshotAdapter):
    """Own an isolated ETH market and ETH/USD Chainlink data stream."""

    asset = "ETH"


__all__ = ["CurrentEthSnapshotAdapter"]
