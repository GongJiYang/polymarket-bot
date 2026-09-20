"""Credential-free official Chainlink price inputs for crypto Up/Down models."""

from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol
from urllib.parse import urlsplit
from urllib.request import getproxies, proxy_bypass

import aiohttp

from polymarket_bot.microstructure.fair_probability import (
    fair_in_progress_terminal_twap_probabilities,
    fair_terminal_twap_probabilities,
)
from polymarket_bot.microstructure.volatility import (
    PriceObservation,
    rolling_realized_volatility,
)

RTDS_WEBSOCKET = "wss://ws-live-data.polymarket.com"
RTDS_SUBSCRIPTIONS = (
    {"topic": "crypto_prices_chainlink", "type": "update"},
)
RTDS_HEARTBEAT_SECONDS = 5
RTDS_RECONNECT_MAX_SECONDS = 5
RTDS_STALE_RECONNECT_SECONDS = 5
MAX_OFFICIAL_PRICE_AGE_SECONDS = 3
MINIMUM_SPOT_HISTORY_SECONDS = 180
MAX_SPOT_HISTORY_GAP_SECONDS = 5
VOLATILITY_FLOOR_PER_SQRT_SECOND = Decimal("0.00005")


def _websocket_proxy() -> str | None:
    host = urlsplit(RTDS_WEBSOCKET).hostname
    if host is None or proxy_bypass(host):
        return None
    proxies = getproxies()
    return proxies.get("https") or proxies.get("http")


class PublicGet(Protocol):
    def json(self, url: str, *, params: dict[str, object] | None = None) -> object: ...


class OfficialPriceError(RuntimeError):
    """Official price input is malformed or cannot be retrieved."""


class OfficialPriceUnavailable(OfficialPriceError):
    """Expected temporary absence of a complete official model input."""



@dataclass(frozen=True, slots=True)
class TerminalProbabilityEstimate:
    """Terminal probabilities plus auditable volatility/path diagnostics."""

    up_probability: Decimal
    down_probability: Decimal
    raw_volatility_per_sqrt_second: Decimal
    effective_volatility_per_sqrt_second: Decimal
    volatility_floor_active: bool
    twap_observed_seconds: Decimal
    volatility_multiplier: Decimal = Decimal("1")

def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise OfficialPriceError("official price timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _timestamp(value: object) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if abs(seconds) >= 10_000_000_000:
            seconds /= 1000
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError) as error:
            raise OfficialPriceError("official price timestamp is invalid") from error
    if isinstance(value, str):
        try:
            return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError as error:
            raise OfficialPriceError("official price timestamp is invalid") from error
    raise OfficialPriceError("official price timestamp is invalid")


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise OfficialPriceError(f"{name} is not decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise OfficialPriceError(f"{name} is not decimal") from error
    if not result.is_finite() or result <= 0:
        raise OfficialPriceError(f"{name} is out of range")
    return result


def _is_chainlink_source(value: object, *, symbol: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and hostname
        and (hostname == "chain.link" or hostname.endswith(".chain.link"))
        and f"/{symbol.lower()}-usd" in parsed.path.lower()
    )


def _history_is_incomplete(
    prices: tuple[tuple[datetime, Decimal], ...],
    *,
    source_maximum_gap_seconds: int,
) -> bool:
    if not prices:
        return True
    return any(
        current[0] - previous[0] > timedelta(seconds=source_maximum_gap_seconds)
        for previous, current in zip(prices, prices[1:])
    )


def _latest_contiguous_history(
    prices: tuple[tuple[datetime, Decimal], ...],
    *,
    source_maximum_gap_seconds: int,
) -> tuple[tuple[datetime, Decimal], ...]:
    """Return the latest contiguous price segment with sufficient duration."""
    latest: tuple[tuple[datetime, Decimal], ...] = ()
    segment_start = 0
    for index, (previous, current) in enumerate(zip(prices, prices[1:]), start=1):
        if current[0] - previous[0] <= timedelta(seconds=source_maximum_gap_seconds):
            continue
        segment = prices[segment_start:index]
        if segment and segment[-1][0] - segment[0][0] >= timedelta(
            seconds=MINIMUM_SPOT_HISTORY_SECONDS
        ):
            latest = segment
        segment_start = index
    segment = prices[segment_start:]
    if segment and segment[-1][0] - segment[0][0] >= timedelta(
        seconds=MINIMUM_SPOT_HISTORY_SECONDS
    ):
        latest = segment
    return latest




class OfficialChainlinkBuffer:
    """Retain one asset's official Chainlink spot observations."""

    def __init__(self, *, symbol: str) -> None:
        if symbol not in {"btc/usd", "eth/usd", "sol/usd", "xrp/usd", "doge/usd"}:
            raise ValueError("unsupported Chainlink RTDS symbol")
        self._symbol = symbol
        self._condition = threading.Condition()
        self._spot_observations: deque[tuple[datetime, Decimal]] = deque()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._failed = False
        self._connected = False
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"polymarket-official-chainlink-{symbol.split('/')[0]}",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(timeout=10) or self._failed:
            self.close()
            raise OfficialPriceUnavailable("official price stream is unavailable")


    def spot_observations(
        self, *, window_start: datetime, observed_at: datetime
    ) -> tuple[tuple[datetime, Decimal], ...]:
        with self._condition:
            if self._failed or not self._connected:
                raise OfficialPriceUnavailable("official price stream is unavailable")
            return tuple(
                (at, price)
                for at, price in self._spot_observations
                if window_start <= at <= observed_at
            )

    def spot_observation(self, *, observed_at: datetime) -> tuple[datetime, Decimal]:
        with self._condition:
            if self._failed or not self._connected:
                raise OfficialPriceUnavailable("official price stream is unavailable")
            for observation in reversed(self._spot_observations):
                if observation[0] <= observed_at:
                    return observation
        raise OfficialPriceUnavailable("official spot price is unavailable")

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _set_connected(self, connected: bool) -> None:
        with self._condition:
            self._connected = connected
            self._condition.notify_all()

    def _record_observation(
        self,
        target: deque[tuple[datetime, Decimal]],
        observed_at: datetime,
        value: Decimal,
    ) -> None:
        observation = (observed_at, value)
        if not target or observed_at > target[-1][0]:
            target.append(observation)
        elif observed_at == target[-1][0]:
            return
        else:
            index = len(target) - 1
            while index >= 0 and target[index][0] > observed_at:
                index -= 1
            if index >= 0 and target[index][0] == observed_at:
                return
            target.insert(index + 1, observation)

        cutoff = target[-1][0] - timedelta(minutes=20)
        while target and target[0][0] < cutoff:
            target.popleft()

    def _ingest_event(self, raw: object) -> bool:
        if not isinstance(raw, dict) or raw.get("type") != "update":
            return False
        payload = raw.get("payload")
        if (
            not isinstance(payload, dict)
            or payload.get("symbol") != self._symbol
            or raw.get("topic") != "crypto_prices_chainlink"
        ):
            return False
        source_timestamp = raw.get("timestamp")
        if source_timestamp is None:
            source_timestamp = payload.get("timestamp")
        try:
            observed_at = _timestamp(source_timestamp)
            value = _decimal(payload.get("value"), "official price")
        except (OfficialPriceError, InvalidOperation, ValueError):
            return False

        with self._condition:
            self._record_observation(self._spot_observations, observed_at, value)
            self._condition.notify_all()
            self._ready.set()
        return True

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._consume())
        except Exception:
            with self._condition:
                self._failed = True
                self._connected = False
                self._condition.notify_all()
            self._ready.set()

    async def _consume(self) -> None:
        reconnect_delay = 1
        async with aiohttp.ClientSession(trust_env=True) as session:
            while not self._stop.is_set():
                try:
                    await self._consume_connection(session)
                    reconnect_delay = 1
                except (aiohttp.ClientError, TimeoutError, OSError):
                    self._set_connected(False)
                if self._stop.is_set():
                    return
                deadline = asyncio.get_running_loop().time() + reconnect_delay
                while (
                    not self._stop.is_set()
                    and asyncio.get_running_loop().time() < deadline
                ):
                    await asyncio.sleep(0.1)
                reconnect_delay = min(reconnect_delay * 2, RTDS_RECONNECT_MAX_SECONDS)

    async def _consume_connection(self, session: aiohttp.ClientSession) -> None:
        async with session.ws_connect(
            RTDS_WEBSOCKET,
            heartbeat=30,
            proxy=_websocket_proxy(),
        ) as socket:
            await socket.send_json(
                {
                    "action": "subscribe",
                    "subscriptions": list(RTDS_SUBSCRIPTIONS),
                }
            )
            self._set_connected(True)
            loop = asyncio.get_running_loop()
            last_observation = loop.time()
            next_heartbeat = loop.time() + RTDS_HEARTBEAT_SECONDS
            while not self._stop.is_set():
                timeout = max(
                    0.01,
                    min(
                        1,
                        next_heartbeat - asyncio.get_running_loop().time(),
                    ),
                )
                try:
                    message = await socket.receive(timeout=timeout)
                except TimeoutError:
                    message = None

                now = asyncio.get_running_loop().time()
                if now >= next_heartbeat:
                    await socket.send_str("PING")
                    next_heartbeat = now + RTDS_HEARTBEAT_SECONDS

                if message is not None:
                    if message.type is aiohttp.WSMsgType.TEXT:
                        try:
                            if self._ingest_event(json.loads(message.data)):
                                last_observation = loop.time()
                        except json.JSONDecodeError:
                            pass
                    elif message.type in {
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSING,
                        aiohttp.WSMsgType.ERROR,
                    }:
                        raise aiohttp.ClientConnectionError(
                            "official price stream disconnected"
                        )
                if loop.time() - last_observation >= RTDS_STALE_RECONNECT_SECONDS:
                    raise aiohttp.ClientConnectionError("official price stream stalled")
        self._set_connected(False)


class OfficialPriceStream:
    """Bind a market's official threshold to live Chainlink spot observations."""

    def __init__(
        self,
        http: PublicGet,
        live: OfficialChainlinkBuffer,
        *,
        symbol: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if symbol not in {"BTC", "ETH", "SOL", "XRP", "DOGE"}:
            raise ValueError("unsupported Chainlink HTTP symbol")
        self._http = http
        self._live = live
        self._symbol = symbol
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def fetch(
        self, *, slug: str, window_start: datetime, window_end: datetime
    ) -> tuple[
        str,
        Decimal,
        tuple[tuple[datetime, Decimal], ...],
        tuple[datetime, Decimal],
        datetime,
    ]:
        window_start = _utc(window_start)
        window_end = _utc(window_end)
        if window_end <= window_start:
            raise OfficialPriceError("official market window is invalid")
        if not slug.startswith(f"{self._symbol.lower()}-updown-"):
            raise OfficialPriceUnavailable(
                f"market slug is not bound to {self._symbol}"
            )
        market = self._http.json(
            f"https://gamma-api.polymarket.com/markets/slug/{slug}"
        )
        if not isinstance(market, dict):
            raise OfficialPriceError("official market price source is malformed")
        if market.get("slug") != slug:
            raise OfficialPriceError("official market slug does not match request")
        source = market.get("resolutionSource")
        if not _is_chainlink_source(source, symbol=self._symbol):
            raise OfficialPriceUnavailable(
                f"market has no official {self._symbol} Chainlink source"
            )
        if (
            _timestamp(market.get("eventStartTime")) != window_start
            or _timestamp(market.get("endDate")) != window_end
        ):
            raise OfficialPriceError("official market window does not match request")
        config = market.get("cryptoMarketConfig")
        if (
            not isinstance(config, dict)
            or config.get("id") != f"{self._symbol.lower()}-5m-twap-60"
            or config.get("asset") != self._symbol.lower()
            or config.get("duration") != "5m"
            or config.get("twapEnabled") is not True
            or config.get("twapLookbackSeconds") != 60
        ):
            raise OfficialPriceError("official market TWAP configuration is invalid")
        opening_payload = self._http.json(
            "https://polymarket.com/api/crypto/crypto-price",
            params={
                "symbol": self._symbol,
                "eventStartTime": window_start.isoformat().replace("+00:00", "Z"),
                "variant": "fiveminute",
                "endDate": window_end.isoformat().replace("+00:00", "Z"),
                "twapEnabled": "true",
                "twapLookbackSeconds": "60",
            },
        )
        if not isinstance(opening_payload, dict):
            raise OfficialPriceError("official opening price source is malformed")
        opening_value = opening_payload.get("openPrice")
        if opening_value is None and opening_payload.get("incomplete") is True:
            raise OfficialPriceUnavailable(
                "official opening price is not available yet"
            )
        opening = _decimal(opening_value, "official opening price")
        observed_at = _utc(self._clock())
        spot_history = _latest_contiguous_history(
            self._live.spot_observations(
                window_start=observed_at - timedelta(minutes=15),
                observed_at=observed_at,
            ),
            source_maximum_gap_seconds=MAX_SPOT_HISTORY_GAP_SECONDS,
        )
        if (
            not spot_history
            or spot_history[-1][0]
            < observed_at - timedelta(seconds=MAX_OFFICIAL_PRICE_AGE_SECONDS)
        ):
            raise OfficialPriceUnavailable(
                "official live spot volatility history is incomplete"
            )
        return source, opening, spot_history, spot_history[-1], observed_at


def _seconds(value: timedelta) -> Decimal:
    return Decimal(value.days * 86_400 + value.seconds) + (
        Decimal(value.microseconds) / Decimal("1000000")
    )


def _observed_price_seconds(
    prices: tuple[tuple[datetime, Decimal], ...],
    *,
    window_start: datetime,
    observed_at: datetime,
) -> Decimal:
    """Integrate the observed final-TWAP path using trapezoidal segments."""

    before = next(
        ((at, price) for at, price in reversed(prices) if at <= window_start),
        None,
    )
    if before is None:
        raise OfficialPriceUnavailable(
            "official final TWAP observed path is incomplete"
        )
    points = [(window_start, before[1])]
    points.extend(
        (at, price) for at, price in prices if window_start < at <= observed_at
    )
    if points[-1][0] < observed_at:
        points.append((observed_at, points[-1][1]))
    return sum(
        (
            (previous[1] + current[1])
            / Decimal("2")
            * _seconds(current[0] - previous[0])
            for previous, current in zip(points, points[1:])
        ),
        Decimal("0"),
    )


def terminal_probability(
    prices: tuple[tuple[datetime, Decimal], ...],
    *,
    opening: Decimal,
    current_spot: tuple[datetime, Decimal],
    window_end: datetime,
    observed_at: datetime | None = None,
    volatility_multiplier: Decimal = Decimal("1"),
) -> TerminalProbabilityEstimate:
    """Recalculate terminal TWAP probabilities after scaling floored volatility."""

    multiplier = _decimal(volatility_multiplier, "volatility_multiplier")
    spot_at, spot = current_spot
    now = _utc(observed_at or spot_at)
    end = _utc(window_end)
    if now >= end:
        raise OfficialPriceUnavailable("official market window has ended")
    tau = _seconds(end - now)
    if (
        not prices
        or prices[-1][0] < now - timedelta(seconds=MAX_OFFICIAL_PRICE_AGE_SECONDS)
        or prices[-1][0] - prices[0][0]
        < timedelta(seconds=MINIMUM_SPOT_HISTORY_SECONDS)
        or _history_is_incomplete(
            prices,
            source_maximum_gap_seconds=MAX_SPOT_HISTORY_GAP_SECONDS,
        )
    ):
        raise OfficialPriceUnavailable(
            "official live spot volatility history is incomplete"
        )
    observations = tuple(
        PriceObservation(price=price, observed_at=at) for at, price in prices
    )
    estimates = tuple(
        rolling_realized_volatility(
            observations,
            as_of=now,
            window=window,
            minimum_returns=2,
            maximum_gap=timedelta(seconds=MAX_SPOT_HISTORY_GAP_SECONDS),
        )
        for window in (timedelta(minutes=5), timedelta(minutes=15))
    )
    raw_sigma = max(
        (
            estimate.volatility_per_sqrt_second
            for estimate in estimates
            if estimate is not None
        ),
        default=None,
    )
    if raw_sigma is None:
        raise OfficialPriceUnavailable("official spot volatility is unavailable")
    sigma = max(VOLATILITY_FLOOR_PER_SQRT_SECOND, raw_sigma) * multiplier
    twap_observed_seconds = max(Decimal("0"), Decimal("60") - tau)
    if twap_observed_seconds == 0:
        up, down = fair_terminal_twap_probabilities(
            current_price=spot,
            window_open_price=opening,
            sigma_per_sqrt_second=sigma,
            seconds_to_resolution=tau,
        )
    else:
        twap_start = end - timedelta(seconds=60)
        observed_price_seconds = _observed_price_seconds(
            prices,
            window_start=twap_start,
            observed_at=now,
        )
        up, down = fair_in_progress_terminal_twap_probabilities(
            current_price=spot,
            window_open_price=opening,
            sigma_per_sqrt_second=sigma,
            observed_price_seconds=observed_price_seconds,
            observed_seconds=twap_observed_seconds,
        )
    return TerminalProbabilityEstimate(
        up_probability=up,
        down_probability=down,
        raw_volatility_per_sqrt_second=raw_sigma,
        effective_volatility_per_sqrt_second=sigma,
        volatility_floor_active=raw_sigma <= VOLATILITY_FLOOR_PER_SQRT_SECOND,
        twap_observed_seconds=twap_observed_seconds,
        volatility_multiplier=multiplier,
    )


__all__ = [
    "OfficialChainlinkBuffer",
    "OfficialPriceError",
    "OfficialPriceStream",
    "OfficialPriceUnavailable",
    "terminal_probability",
    "TerminalProbabilityEstimate",
]
