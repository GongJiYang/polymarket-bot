from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from polymarket_bot.adapters.alt_public_data import (
    CurrentDogeSnapshotAdapter,
    CurrentSolSnapshotAdapter,
    CurrentXrpSnapshotAdapter,
)
from polymarket_bot.adapters.eth_public_data import CurrentEthSnapshotAdapter
from polymarket_bot.adapters.public_data import (
    CLOB,
    GAMMA,
    CurrentBtcSnapshotAdapter,
    PublicDataError,
)
from polymarket_bot.microstructure.models import MarketInterval, OutcomeSide

NOW = datetime(2026, 8, 25, 12, 1, tzinfo=timezone.utc)
START = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc)
END = datetime(2026, 8, 25, 12, 5, tzinfo=timezone.utc)


class Http:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def json(self, url: str, *, params: dict[str, object] | None = None) -> object:
        self.calls.append((url, params))
        if url == f"{GAMMA}/markets/slug/btc-updown-5m-1787659200":
            return {
                "question": "Bitcoin Up or Down - August 25, 8:00AM-8:05AM ET",
                "description": "Resolves Up or Down from the Bitcoin Chainlink feed.",
                "outcomes": '["Up", "Down"]',
                "clobTokenIds": '["up", "down"]',
                "eventStartTime": "2026-08-25T12:00:00Z",
                "endDate": "2026-08-25T12:05:00Z",
                "conditionId": "condition",
                "active": True,
                "closed": False,
            }
        if url == f"{CLOB}/book":
            token = str(params["token_id"])
            return {
                "asset_id": token,
                "market": "condition",
                "bids": [{"price": "0.44", "size": "20"}],
                "asks": [{"price": "0.45", "size": "20"}],
                "tick_size": "0.01",
                "timestamp": str(int(NOW.timestamp() * 1000)),
            }
        if url == f"{CLOB}/clob-markets/condition":
            return {
                "c": "condition",
                "ao": True,
                "fd": {"r": "0", "e": "0"},
                "mos": "5",
            }
        raise AssertionError(url)


class Prices:
    def fetch(self, *, slug: str, window_start: datetime, window_end: datetime):
        assert slug == "btc-updown-5m-1787659200"
        assert (window_start, window_end) == (START, END)
        return (
            "https://data.chain.link/streams/btc-usd",
            Decimal("70000"),
            ((START, Decimal("70000")), (NOW, Decimal("70010"))),
            (NOW, Decimal("70010")),
            NOW,
        )


def test_current_snapshot_adapter_assembles_typed_context() -> None:
    http = Http()
    adapter = CurrentBtcSnapshotAdapter(
        interval=MarketInterval.FIVE_MINUTES,
        threshold=Decimal("0.04"),
        quantity=Decimal("5"),
        http=http,
        official_prices=Prices(),
        clock=lambda: NOW,
    )
    context = adapter.snapshot()
    assert context.market.market_id == "condition"
    assert context.up_book.token_id == "up"
    assert context.down_book.token_id == "down"
    assert context.fee.minimum_size == Decimal("5")
    assert context.current_spot == (NOW, Decimal("70010"))
    assert all(url.startswith((GAMMA, CLOB)) for url, _ in http.calls)


def test_final_snapshot_refetches_every_public_decision_input() -> None:
    http = Http()
    adapter = CurrentBtcSnapshotAdapter(
        interval=MarketInterval.FIVE_MINUTES,
        threshold=Decimal("0.04"),
        quantity=Decimal("5"),
        http=http,
        official_prices=Prices(),
        clock=lambda: NOW,
    )
    initial = adapter.snapshot()
    http.calls.clear()

    final = adapter.final_snapshot(initial)

    assert final.market == initial.market
    assert final.up_book.token_id == "up"
    assert final.down_book.token_id == "down"
    assert final.current_spot == (NOW, Decimal("70010"))
    assert tuple(name for name, _ in final.observed_at) == (
        "official_price",
        "market",
        "up_book",
        "down_book",
        "fee",
    )
    assert len(http.calls) == 4
    assert (
        f"{GAMMA}/markets/slug/btc-updown-5m-1787659200",
        None,
    ) in http.calls
    assert (f"{CLOB}/book", {"token_id": "up"}) in http.calls
    assert (f"{CLOB}/book", {"token_id": "down"}) in http.calls
    assert (f"{CLOB}/clob-markets/condition", None) in http.calls


def test_current_snapshot_adapter_refetches_selected_book() -> None:
    http = Http()
    adapter = CurrentBtcSnapshotAdapter(
        interval=MarketInterval.FIVE_MINUTES,
        threshold=Decimal("0.04"),
        quantity=Decimal("5"),
        http=http,
        official_prices=Prices(),
        clock=lambda: NOW,
    )
    context = adapter.snapshot()
    http.calls.clear()

    book = adapter.book(context.market, OutcomeSide.DOWN)

    assert book.token_id == "down"
    assert http.calls == [(f"{CLOB}/book", {"token_id": "down"})]


def test_current_snapshot_adapter_tolerates_small_exchange_clock_skew() -> None:
    class AheadHttp(Http):
        def json(self, url: str, *, params: dict[str, object] | None = None) -> object:
            payload = super().json(url, params=params)
            if url == f"{CLOB}/book":
                assert isinstance(payload, dict)
                payload["timestamp"] = str(
                    int((NOW + timedelta(seconds=1)).timestamp() * 1000)
                )
            return payload

    context = CurrentBtcSnapshotAdapter(
        interval=MarketInterval.FIVE_MINUTES,
        threshold=Decimal("0.04"),
        quantity=Decimal("5"),
        http=AheadHttp(),
        official_prices=Prices(),
        clock=lambda: NOW,
    ).snapshot()

    assert context.up_book.exchange_at == NOW
    assert context.down_book.exchange_at == NOW


def test_current_snapshot_adapter_rejects_large_exchange_clock_skew() -> None:
    class AheadHttp(Http):
        def json(self, url: str, *, params: dict[str, object] | None = None) -> object:
            payload = super().json(url, params=params)
            if url == f"{CLOB}/book":
                assert isinstance(payload, dict)
                payload["timestamp"] = str(
                    int((NOW + timedelta(seconds=3)).timestamp() * 1000)
                )
            return payload

    adapter = CurrentBtcSnapshotAdapter(
        interval=MarketInterval.FIVE_MINUTES,
        threshold=Decimal("0.04"),
        quantity=Decimal("5"),
        http=AheadHttp(),
        official_prices=Prices(),
        clock=lambda: NOW,
    )

    with pytest.raises(PublicDataError, match="clock skew"):
        adapter.snapshot()


def test_eth_snapshot_is_bound_to_eth_market_and_price_source() -> None:
    class EthHttp(Http):
        def json(self, url: str, *, params: dict[str, object] | None = None) -> object:
            if url == f"{GAMMA}/markets/slug/eth-updown-5m-1787659200":
                self.calls.append((url, params))
                return {
                    "question": "Ethereum Up or Down - August 25, 8:00AM-8:05AM ET",
                    "description": "Resolves Up or Down from the Ethereum Chainlink feed.",
                    "outcomes": '["Up", "Down"]',
                    "clobTokenIds": '["eth-up", "eth-down"]',
                    "eventStartTime": "2026-08-25T12:00:00Z",
                    "endDate": "2026-08-25T12:05:00Z",
                    "conditionId": "eth-condition",
                    "active": True,
                    "closed": False,
                }
            payload = super().json(url, params=params)
            if url == f"{CLOB}/book":
                assert isinstance(payload, dict)
                payload["market"] = "eth-condition"
            elif url == f"{CLOB}/clob-markets/eth-condition":
                raise AssertionError("unreachable")
            return payload

    class EthPrices:
        def fetch(self, *, slug: str, window_start: datetime, window_end: datetime):
            assert slug == "eth-updown-5m-1787659200"
            return (
                "https://data.chain.link/streams/eth-usd",
                Decimal("4500"),
                ((START, Decimal("4500")), (NOW, Decimal("4501"))),
                (NOW, Decimal("4501")),
                NOW,
            )

    http = EthHttp()
    original_json = http.json

    def eth_json(url: str, *, params: dict[str, object] | None = None) -> object:
        if url == f"{CLOB}/clob-markets/eth-condition":
            http.calls.append((url, params))
            return {
                "c": "eth-condition",
                "ao": True,
                "fd": {"r": "0", "e": "0"},
                "mos": "5",
            }
        return original_json(url, params=params)

    http.json = eth_json  # type: ignore[method-assign]
    context = CurrentEthSnapshotAdapter(
        interval=MarketInterval.FIVE_MINUTES,
        threshold=Decimal("0.04"),
        quantity=Decimal("5"),
        http=http,
        official_prices=EthPrices(),
        clock=lambda: NOW,
    ).snapshot()

    assert context.market.asset == "ETH"
    assert context.market.market_id == "eth-condition"
    assert context.source == "https://data.chain.link/streams/eth-usd"
    assert context.up_book.token_id == "eth-up"
    assert context.down_book.token_id == "eth-down"


def test_eth_adapter_rejects_btc_market_payload() -> None:
    class BtcPayloadAtEthSlug(Http):
        def json(self, url: str, *, params: dict[str, object] | None = None) -> object:
            if url == f"{GAMMA}/markets/slug/eth-updown-5m-1787659200":
                url = f"{GAMMA}/markets/slug/btc-updown-5m-1787659200"
            return super().json(url, params=params)

    adapter = CurrentEthSnapshotAdapter(
        interval=MarketInterval.FIVE_MINUTES,
        threshold=Decimal("0.04"),
        quantity=Decimal("5"),
        http=BtcPayloadAtEthSlug(),
        official_prices=Prices(),
        clock=lambda: NOW,
    )

    with pytest.raises(PublicDataError, match="not an ETH Up/Down market"):
        adapter.snapshot()


@pytest.mark.parametrize(
    ("adapter_type", "asset"),
    (
        (CurrentSolSnapshotAdapter, "SOL"),
        (CurrentXrpSnapshotAdapter, "XRP"),
        (CurrentDogeSnapshotAdapter, "DOGE"),
    ),
)
def test_alt_snapshot_adapters_bind_one_asset_family(adapter_type, asset: str) -> None:
    assert adapter_type.asset == asset
