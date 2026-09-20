"""Isolated public-data adapters for supported altcoin market families."""

from polymarket_bot.adapters.public_data import CurrentBtcSnapshotAdapter


class CurrentSolSnapshotAdapter(CurrentBtcSnapshotAdapter):
    asset = "SOL"


class CurrentXrpSnapshotAdapter(CurrentBtcSnapshotAdapter):
    asset = "XRP"


class CurrentDogeSnapshotAdapter(CurrentBtcSnapshotAdapter):
    asset = "DOGE"


__all__ = [
    "CurrentDogeSnapshotAdapter",
    "CurrentSolSnapshotAdapter",
    "CurrentXrpSnapshotAdapter",
]
