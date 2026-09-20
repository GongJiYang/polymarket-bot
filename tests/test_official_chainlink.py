from __future__ import annotations

import asyncio

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import aiohttp
import pytest

from polymarket_bot.live import official_chainlink
from polymarket_bot.live.official_chainlink import (
    OfficialChainlinkBuffer,
    OfficialPriceError,
    OfficialPriceStream,
    OfficialPriceUnavailable,
    terminal_probability,
)
from polymarket_bot.microstructure.fair_probability import (
    fair_in_progress_terminal_twap_probabilities,
    fair_terminal_twap_probabilities,
)


@pytest.mark.parametrize(
    ("http_symbol", "rtds_symbol"),
    (
        ("SOL", "sol/usd"),
        ("XRP", "xrp/usd"),
        ("DOGE", "doge/usd"),
    ),
)
def test_alt_symbols_are_accepted_by_official_sources(
    http_symbol: str, rtds_symbol: str
) -> None:
    OfficialPriceStream(MarketHttp(), LivePrices(()), symbol=http_symbol)
    OfficialChainlinkBuffer(symbol=rtds_symbol)


NOW = datetime(2026, 8, 25, 16, 50, tzinfo=timezone.utc)


class MarketHttp:
    def json(self, url: str, *, params: dict[str, object] | None = None) -> object:
        if "/markets/slug/" in url:
            assert params is None
            return {
                "slug": url.rsplit("/", 1)[-1],
                "resolutionSource": "https://data.chain.link/streams/btc-usd",
                "eventStartTime": NOW.isoformat(),
                "endDate": (NOW + timedelta(minutes=5)).isoformat(),
                "priceToBeat": "1",
                "cryptoMarketConfig": {
                    "id": "btc-5m-twap-60",
                    "asset": "btc",
                    "duration": "5m",
                    "twapEnabled": True,
                    "twapLookbackSeconds": 60,
                },
            }
        assert url == "https://polymarket.com/api/crypto/crypto-price"
        assert params == {
            "symbol": "BTC",
            "eventStartTime": "2026-08-25T16:50:00Z",
            "variant": "fiveminute",
            "endDate": "2026-08-25T16:55:00Z",
            "twapEnabled": "true",
            "twapLookbackSeconds": "60",
        }
        return {"openPrice": "71399"}


class LivePrices:
    def __init__(self, observations: tuple[tuple[datetime, Decimal], ...]) -> None:
        self._observations = observations


    def spot_observations(
        self, *, window_start: datetime, observed_at: datetime
    ) -> tuple[tuple[datetime, Decimal], ...]:
        return tuple(
            (
                observed_at - timedelta(seconds=180 - offset),
                Decimal(71_420) + Decimal(offset % 3),
            )
            for offset in range(181)
            if window_start <= observed_at - timedelta(seconds=180 - offset)
        )

    def spot_observation(self, *, observed_at: datetime) -> tuple[datetime, Decimal]:
        return observed_at, Decimal("71420")


class GappedLivePrices(LivePrices):
    def spot_observations(
        self, *, window_start: datetime, observed_at: datetime
    ) -> tuple[tuple[datetime, Decimal], ...]:
        old_segment = (
            (observed_at - timedelta(minutes=10), Decimal("71400")),
            (
                observed_at - timedelta(minutes=10) + timedelta(seconds=1),
                Decimal("71401"),
            ),
        )
        current_segment = tuple(
            (
                observed_at - timedelta(seconds=180 - offset),
                Decimal(71_420) + Decimal(offset % 3),
            )
            for offset in range(181)
        )
        return old_segment + current_segment


class SilentSocket:
    async def send_json(self, _payload: object) -> None:
        return None

    async def send_str(self, _payload: str) -> None:
        return None

    async def receive(self, *, timeout: float) -> object:
        raise TimeoutError


class SocketContext:
    async def __aenter__(self) -> SilentSocket:
        return SilentSocket()

    async def __aexit__(self, *_args: object) -> None:
        return None


class SilentSession:
    def ws_connect(self, *_args: object, **_kwargs: object) -> SocketContext:
        return SocketContext()


def test_buffer_disconnects_a_stream_that_stops_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(official_chainlink, "RTDS_STALE_RECONNECT_SECONDS", 0)

    with pytest.raises(aiohttp.ClientConnectionError, match="stream stalled"):
        asyncio.run(
            OfficialChainlinkBuffer(symbol="btc/usd")._consume_connection(  # type: ignore[arg-type]
                SilentSession()
            )
        )


def test_stream_uses_market_price_to_beat_and_live_chainlink_spot() -> None:
    observed_at = NOW + timedelta(seconds=119)
    live = LivePrices(())

    source, opening, prices, spot, fetched_at = OfficialPriceStream(
        MarketHttp(), live, symbol="BTC", clock=lambda: observed_at
    ).fetch(
        slug="btc-updown-5m-test",
        window_start=NOW,
        window_end=NOW + timedelta(minutes=5),
    )

    assert source == "https://data.chain.link/streams/btc-usd"
    assert opening == Decimal("71399")
    assert prices[-1][0] == observed_at
    assert spot == (observed_at, Decimal("71420"))
    assert fetched_at == observed_at
    assert prices[0][0] == observed_at - timedelta(seconds=180)


def test_stream_recovers_after_a_gap_before_complete_current_spot_history() -> None:
    observed_at = NOW + timedelta(seconds=119)
    live = GappedLivePrices(())

    _, _, spot_history, spot, _ = OfficialPriceStream(
        MarketHttp(), live, symbol="BTC", clock=lambda: observed_at
    ).fetch(
        slug="btc-updown-5m-test",
        window_start=NOW,
        window_end=NOW + timedelta(minutes=5),
    )

    assert spot_history[0][0] == observed_at - timedelta(seconds=180)
    assert spot == (observed_at, Decimal("71420"))


def test_terminal_probability_stresses_quiet_spot_history() -> None:
    observed_at = NOW + timedelta(minutes=4)
    prices = tuple(
        (
            observed_at - timedelta(seconds=180 - offset),
            Decimal(78_000) + Decimal(offset % 3),
        )
        for offset in range(181)
    )

    estimate = terminal_probability(
        prices,
        opening=Decimal(78_000),
        current_spot=(observed_at, Decimal(78_060)),
        window_end=observed_at + timedelta(seconds=60),
    )

    assert estimate.effective_volatility_per_sqrt_second == Decimal("0.00005")
    assert estimate.raw_volatility_per_sqrt_second < Decimal("0.00010")
    assert estimate.volatility_floor_active is True
    assert estimate.twap_observed_seconds == 0
    assert estimate.up_probability > Decimal("0.5")
    assert estimate.up_probability + estimate.down_probability == Decimal(1)


@pytest.mark.parametrize("seconds_remaining", [120, 60, 40])
@pytest.mark.parametrize("price_amplitude", [Decimal("1"), Decimal("20")])
def test_terminal_probability_recalculates_stressed_full_and_partial_twap(
    seconds_remaining: int, price_amplitude: Decimal
) -> None:
    observed_at = NOW + timedelta(minutes=4)
    prices = tuple(
        (
            observed_at - timedelta(seconds=180 - offset),
            Decimal("78010") + price_amplitude * (offset % 2),
        )
        for offset in range(181)
    )
    inputs = {
        "opening": Decimal("78000"),
        "current_spot": prices[-1],
        "window_end": observed_at + timedelta(seconds=seconds_remaining),
    }
    baseline = terminal_probability(prices, **inputs)
    explicit_baseline = terminal_probability(
        prices, **inputs, volatility_multiplier=Decimal("1")
    )
    stressed = terminal_probability(
        prices, **inputs, volatility_multiplier=Decimal("1.25")
    )
    assert baseline == explicit_baseline
    assert stressed.volatility_multiplier == Decimal("1.25")
    assert (
        stressed.effective_volatility_per_sqrt_second
        == baseline.effective_volatility_per_sqrt_second * Decimal("1.25")
    )
    assert stressed.raw_volatility_per_sqrt_second == baseline.raw_volatility_per_sqrt_second
    assert stressed.volatility_floor_active is (price_amplitude == 1)

    for estimate in (baseline, stressed):
        if seconds_remaining >= 60:
            expected = fair_terminal_twap_probabilities(
                current_price=prices[-1][1],
                window_open_price=inputs["opening"],
                sigma_per_sqrt_second=estimate.effective_volatility_per_sqrt_second,
                seconds_to_resolution=Decimal(seconds_remaining),
            )
        else:
            # Twenty observed one-second trapezoids of an alternating price path.
            observed_integral = (
                Decimal("78010") + price_amplitude / Decimal("2")
            ) * Decimal("20")
            expected = fair_in_progress_terminal_twap_probabilities(
                current_price=prices[-1][1],
                window_open_price=inputs["opening"],
                sigma_per_sqrt_second=estimate.effective_volatility_per_sqrt_second,
                observed_price_seconds=observed_integral,
                observed_seconds=Decimal("20"),
            )
        assert (estimate.up_probability, estimate.down_probability) == expected
    assert Decimal("0.5") < stressed.up_probability < baseline.up_probability
    assert stressed.up_probability + stressed.down_probability == Decimal("1")


@pytest.mark.parametrize(
    "multiplier",
    [
        Decimal("0"),
        Decimal("-1"),
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        True,
        None,
        "invalid",
    ],
)
def test_terminal_probability_rejects_invalid_volatility_multiplier(multiplier) -> None:
    prices = tuple(
        (NOW - timedelta(seconds=180 - offset), Decimal("78000"))
        for offset in range(181)
    )
    with pytest.raises(OfficialPriceError, match="volatility_multiplier"):
        terminal_probability(
            prices,
            opening=Decimal("78000"),
            current_spot=prices[-1],
            window_end=NOW + timedelta(seconds=60),
            volatility_multiplier=multiplier,
        )


def test_terminal_probability_rejects_sparse_spot_history() -> None:
    observed_at = NOW + timedelta(minutes=4)
    prices = tuple(
        (
            observed_at - timedelta(seconds=180 - 10 * offset),
            Decimal(78_000 + offset),
        )
        for offset in range(19)
    )

    with pytest.raises(
        OfficialPriceUnavailable,
        match="live spot volatility history",
    ):
        terminal_probability(
            prices,
            opening=Decimal(78_000),
            current_spot=(observed_at, Decimal(78_010)),
            window_end=observed_at + timedelta(seconds=60),
        )


def test_stream_rejects_missing_official_opening_price() -> None:
    class MissingThresholdHttp(MarketHttp):
        def json(
            self, url: str, *, params: dict[str, object] | None = None
        ) -> object:
            payload = super().json(url, params=params)
            if "/api/crypto/crypto-price" in url:
                assert isinstance(payload, dict)
                payload.pop("openPrice")
            return payload

    observed_at = NOW + timedelta(seconds=119)
    with pytest.raises(OfficialPriceError, match="official opening price"):
        OfficialPriceStream(
            MissingThresholdHttp(),
            LivePrices(()),
            symbol="BTC",
            clock=lambda: observed_at,
        ).fetch(
            slug="btc-updown-5m-test",
            window_start=NOW,
            window_end=NOW + timedelta(minutes=5),
        )



def test_stream_waits_for_incomplete_official_opening_price() -> None:
    class IncompleteThresholdHttp(MarketHttp):
        def json(
            self, url: str, *, params: dict[str, object] | None = None
        ) -> object:
            payload = super().json(url, params=params)
            if "/api/crypto/crypto-price" in url:
                assert isinstance(payload, dict)
                payload["openPrice"] = None
                payload["incomplete"] = True
            return payload

    observed_at = NOW + timedelta(seconds=1)
    with pytest.raises(
        OfficialPriceUnavailable, match="opening price is not available yet"
    ):
        OfficialPriceStream(
            IncompleteThresholdHttp(),
            LivePrices(()),
            symbol="BTC",
            clock=lambda: observed_at,
        ).fetch(
            slug="btc-updown-5m-test",
            window_start=NOW,
            window_end=NOW + timedelta(minutes=5),
        )


def test_stream_rejects_market_slug_identity_mismatch() -> None:
    class WrongSlugHttp(MarketHttp):
        def json(
            self, url: str, *, params: dict[str, object] | None = None
        ) -> object:
            payload = super().json(url, params=params)
            assert isinstance(payload, dict)
            payload["slug"] = "btc-updown-5m-different"
            return payload

    observed_at = NOW + timedelta(seconds=119)
    with pytest.raises(OfficialPriceError, match="slug does not match"):
        OfficialPriceStream(
            WrongSlugHttp(),
            LivePrices(()),
            symbol="BTC",
            clock=lambda: observed_at,
        ).fetch(
            slug="btc-updown-5m-test",
            window_start=NOW,
            window_end=NOW + timedelta(minutes=5),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("eventStartTime", "2026-08-25T16:51:00Z", "window does not match"),
        (
            "cryptoMarketConfig",
            {
                "id": "btc-5m-twap-60",
                "asset": "eth",
                "duration": "5m",
                "twapEnabled": True,
                "twapLookbackSeconds": 60,
            },
            "TWAP configuration is invalid",
        ),
    ),
)
def test_stream_rejects_market_contract_mismatch(
    field: str, value: object, message: str
) -> None:
    class WrongMarketHttp(MarketHttp):
        def json(
            self, url: str, *, params: dict[str, object] | None = None
        ) -> object:
            payload = super().json(url, params=params)
            assert isinstance(payload, dict)
            payload[field] = value
            return payload

    observed_at = NOW + timedelta(seconds=119)
    with pytest.raises(OfficialPriceError, match=message):
        OfficialPriceStream(
            WrongMarketHttp(),
            LivePrices(()),
            symbol="BTC",
            clock=lambda: observed_at,
        ).fetch(
            slug="btc-updown-5m-test",
            window_start=NOW,
            window_end=NOW + timedelta(minutes=5),
        )


def test_terminal_probability_uses_observed_final_twap_path() -> None:
    observed_at = NOW + timedelta(minutes=4, seconds=20)
    twap_start = NOW + timedelta(minutes=4)

    def prices(final_path_price: Decimal) -> tuple[tuple[datetime, Decimal], ...]:
        return tuple(
            (
                observed_at - timedelta(seconds=180 - offset),
                (
                    final_path_price
                    if observed_at - timedelta(seconds=180 - offset) >= twap_start
                    else Decimal(78_000)
                ),
            )
            for offset in range(181)
        )

    high_path = terminal_probability(
        prices(Decimal(78_100)),
        opening=Decimal(78_000),
        current_spot=(observed_at, Decimal(78_000)),
        window_end=NOW + timedelta(minutes=5),
    )
    low_path = terminal_probability(
        prices(Decimal(77_900)),
        opening=Decimal(78_000),
        current_spot=(observed_at, Decimal(78_000)),
        window_end=NOW + timedelta(minutes=5),
    )

    assert high_path.twap_observed_seconds == Decimal("20")
    assert low_path.twap_observed_seconds == Decimal("20")
    assert high_path.up_probability > low_path.up_probability
    assert (
        high_path.effective_volatility_per_sqrt_second
        == high_path.raw_volatility_per_sqrt_second
    )
    assert high_path.volatility_floor_active is False
    assert high_path.up_probability + high_path.down_probability == Decimal(1)
    assert low_path.up_probability + low_path.down_probability == Decimal(1)

def test_eth_stream_binds_eth_threshold_stream_and_spot_symbol() -> None:
    class EthMarketHttp(MarketHttp):
        def json(
            self, url: str, *, params: dict[str, object] | None = None
        ) -> object:
            if "/markets/slug/" in url:
                assert params is None
                return {
                    "slug": url.rsplit("/", 1)[-1],
                    "resolutionSource": (
                        "https://data.chain.link/streams/eth-usd-twap-60s-streams"
                    ),
                    "eventStartTime": NOW.isoformat(),
                    "endDate": (NOW + timedelta(minutes=5)).isoformat(),
                    "cryptoMarketConfig": {
                        "id": "eth-5m-twap-60",
                        "asset": "eth",
                        "duration": "5m",
                        "twapEnabled": True,
                        "twapLookbackSeconds": 60,
                    },
                }
            assert url == "https://polymarket.com/api/crypto/crypto-price"
            assert params is not None
            assert params["symbol"] == "ETH"
            return {"openPrice": "4499"}

    observed_at = NOW + timedelta(seconds=119)
    live = LivePrices(())

    _, opening, _, _, _ = OfficialPriceStream(
        EthMarketHttp(), live, symbol="ETH", clock=lambda: observed_at
    ).fetch(
        slug="eth-updown-5m-test",
        window_start=NOW,
        window_end=NOW + timedelta(minutes=5),
    )

    assert opening == Decimal("4499")
    eth_buffer = OfficialChainlinkBuffer(symbol="eth/usd")
    assert eth_buffer._ingest_event(
        {
            "type": "update",
            "topic": "crypto_prices_chainlink",
            "timestamp": int(observed_at.timestamp() * 1_000),
            "payload": {"symbol": "eth/usd", "value": "4500"},
        }
    )
    assert not eth_buffer._ingest_event(
        {
            "type": "update",
            "topic": "crypto_prices_twap_sixty",
            "timestamp": int(observed_at.timestamp() * 1_000),
            "payload": {
                "symbol": "eth/usd",
                "window_s": 60,
                "value": "4500",
            },
        }
    )
    with pytest.raises(
        OfficialPriceUnavailable,
        match="market has no official ETH Chainlink source",
    ):
        OfficialPriceStream(
            MarketHttp(), live, symbol="ETH", clock=lambda: observed_at
        ).fetch(
            slug="eth-updown-5m-test",
            window_start=NOW,
            window_end=NOW + timedelta(minutes=5),
        )
