"""GET-only Polymarket and Chainlink adapter for typed crypto snapshots."""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from polymarket_bot.contracts import FeeMetadata, StrategyContext
from polymarket_bot.live.official_chainlink import (
    OfficialChainlinkBuffer,
    OfficialPriceStream,
)
from polymarket_bot.microstructure.models import (
    BookLevel,
    MarketInterval,
    MarketMetadata,
    OrderBook,
    OutcomeSide,
    require_utc,
)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
MAX_BOOK_AGE = timedelta(seconds=2)
MAX_CLOCK_SKEW = timedelta(seconds=2)
_ASSET_PATTERNS = {
    "BTC": re.compile(r"\b(?:btc|bitcoin)\b", re.IGNORECASE),
    "ETH": re.compile(r"\b(?:eth|ethereum)\b", re.IGNORECASE),
    "SOL": re.compile(r"\b(?:sol|solana)\b", re.IGNORECASE),
    "XRP": re.compile(r"\bxrp\b", re.IGNORECASE),
    "DOGE": re.compile(r"\b(?:doge|dogecoin)\b", re.IGNORECASE),
}
_UP_DOWN = re.compile(r"\bup\b.*\bdown\b|\bdown\b.*\bup\b", re.IGNORECASE | re.DOTALL)


class PublicDataError(RuntimeError):
    """Public data cannot be represented without guessing."""


class PublicGetProtocol(Protocol):
    def json(self, url: str, *, params: dict[str, object] | None = None) -> object: ...


class OfficialPriceProtocol(Protocol):
    def fetch(
        self, *, slug: str, window_start: datetime, window_end: datetime
    ) -> tuple[
        str,
        Decimal,
        tuple[tuple[datetime, Decimal], ...],
        tuple[datetime, Decimal],
        datetime,
    ]: ...


class PublicGet:
    """Bounded anonymous transport exposing GET only."""

    def __init__(self, timeout: float = 8.0) -> None:
        retry = Retry(
            total=2,
            connect=2,
            read=2,
            status=2,
            allowed_methods=frozenset({"GET"}),
            status_forcelist=(429, 500, 502, 503, 504),
            backoff_factor=0.25,
        )
        self._session = requests.Session()
        self._session.mount("https://", HTTPAdapter(max_retries=retry))
        self._timeout = timeout

    def json(self, url: str, *, params: dict[str, object] | None = None) -> object:
        try:
            response = self._session.get(url, params=params, timeout=self._timeout)
            response.raise_for_status()
            return json.loads(response.text, parse_float=Decimal)
        except (requests.RequestException, ValueError) as exc:
            raise PublicDataError(f"public GET failed: {url}") from exc

    def close(self) -> None:
        self._session.close()


class CurrentBtcSnapshotAdapter:
    asset = "BTC"
    """Assemble one immutable strategy context from public official sources."""

    def __init__(
        self,
        *,
        interval: MarketInterval,
        threshold: Decimal,
        quantity: Decimal,
        http: PublicGetProtocol,
        official_prices: OfficialPriceProtocol,
        clock: Callable[[], datetime],
        target_all_in_debit: Decimal | None = None,
        close: Callable[[], None] | None = None,
    ) -> None:
        if threshold <= 0 or quantity <= 0:
            raise ValueError("threshold and quantity must be positive")
        if target_all_in_debit is not None and target_all_in_debit <= 0:
            raise ValueError("target_all_in_debit must be positive")
        self._interval = interval
        self._threshold = threshold
        self._quantity = quantity
        self._target_all_in_debit = target_all_in_debit
        self._http = http
        self._official_prices = official_prices
        self._clock = clock
        self._close = close

    @classmethod
    def connect(
        cls,
        *,
        interval: MarketInterval,
        threshold: Decimal,
        quantity: Decimal,
        target_all_in_debit: Decimal | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> "CurrentBtcSnapshotAdapter":
        resolved_clock = clock or (lambda: datetime.now(timezone.utc))
        http = PublicGet()
        live = OfficialChainlinkBuffer(symbol=f"{cls.asset.lower()}/usd")
        try:
            live.start()
        except BaseException:
            http.close()
            raise
        official = OfficialPriceStream(
            http, live, symbol=cls.asset, clock=resolved_clock
        )

        def close() -> None:
            live.close()
            http.close()

        return cls(
            interval=interval,
            threshold=threshold,
            quantity=quantity,
            target_all_in_debit=target_all_in_debit,
            http=http,
            official_prices=official,
            clock=resolved_clock,
            close=close,
        )

    def snapshot(self) -> StrategyContext:
        now = require_utc(self._clock())
        epoch = int(now.timestamp()) // self._interval.seconds * self._interval.seconds
        slug = f"{self.asset.lower()}-updown-{self._interval.value}-{epoch}"
        market = _parse_market(
            _object(self._http.json(f"{GAMMA}/markets/slug/{slug}"), "market"),
            asset=self.asset,
        )
        if (
            market.interval is not self._interval
            or not market.window_start <= now < market.window_end
        ):
            raise PublicDataError("current market identity or window changed")
        up = _book(self._http, market, OutcomeSide.UP, self._clock)
        down = _book(self._http, market, OutcomeSide.DOWN, self._clock)
        observed_at = require_utc(self._clock())
        _require_fresh_books(up, down, observed_at)
        fee = _fee(self._http, market)
        source, opening, prices, spot, _ = self._official_prices.fetch(
            slug=slug,
            window_start=market.window_start,
            window_end=market.window_end,
        )
        return StrategyContext(
            market=market,
            up_book=up,
            down_book=down,
            fee=fee,
            source=source,
            opening=opening,
            prices=prices,
            current_spot=spot,
            threshold=self._threshold,
            quantity=self._quantity,
            target_all_in_debit=self._target_all_in_debit,
        )

    def final_snapshot(self, initial: StrategyContext) -> StrategyContext:
        """Refetch every public decision input in one bounded observation window."""
        now = require_utc(self._clock())
        market = initial.market
        if (
            market.interval is not self._interval
            or not market.window_start <= now < market.window_end
        ):
            raise PublicDataError("final market window changed")
        slug = (
            f"{self.asset.lower()}-updown-{self._interval.value}-"
            f"{int(market.window_start.timestamp())}"
        )
        source, opening, prices, spot, _ = self._official_prices.fetch(
            slug=slug,
            window_start=market.window_start,
            window_end=market.window_end,
        )
        price_at = require_utc(self._clock())

        def observed(call: Callable[..., Any], *args: Any) -> tuple[Any, datetime]:
            value = call(*args)
            return value, require_utc(self._clock())

        with ThreadPoolExecutor(max_workers=4) as executor:
            market_future = executor.submit(
                observed,
                self._http.json,
                f"{GAMMA}/markets/slug/{slug}",
            )
            up_future = executor.submit(
                observed, _book, self._http, market, OutcomeSide.UP, self._clock
            )
            down_future = executor.submit(
                observed, _book, self._http, market, OutcomeSide.DOWN, self._clock
            )
            fee_future = executor.submit(observed, _fee, self._http, market)
            market_payload, market_at = market_future.result()
            up_value, up_at = up_future.result()
            down_value, down_at = down_future.result()
            fee_value, fee_at = fee_future.result()

        final_market = _parse_market(
            _object(market_payload, "market"), asset=self.asset
        )
        if final_market != market:
            raise PublicDataError("final market metadata changed")
        up = up_value
        down = down_value
        observed_at = require_utc(self._clock())
        _require_fresh_books(up, down, observed_at)
        return StrategyContext(
            market=final_market,
            up_book=up,
            down_book=down,
            fee=fee_value,
            source=source,
            opening=opening,
            prices=prices,
            current_spot=spot,
            threshold=self._threshold,
            quantity=self._quantity,
            target_all_in_debit=self._target_all_in_debit,
            observed_at=(
                ("official_price", price_at),
                ("market", market_at),
                ("up_book", up_at),
                ("down_book", down_at),
                ("fee", fee_at),
            ),
        )

    def book(self, market: MarketMetadata, outcome: OutcomeSide) -> OrderBook:
        """Fetch one fresh official CLOB book for the final pre-POST gate."""
        book = _book(self._http, market, outcome, self._clock)
        _require_fresh_book(book, require_utc(self._clock()))
        return book

    def fee(self, market: MarketMetadata) -> FeeMetadata:
        """Refresh official execution fees for a held market."""
        return _fee(self._http, market)

    def close(self) -> None:
        if self._close is not None:
            close, self._close = self._close, None
            close()

    def __enter__(self) -> "CurrentBtcSnapshotAdapter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _object(value: object, name: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise PublicDataError(f"{name} is not an object")
    return value


def _string(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if type(value) is not str or not value.strip():
        raise PublicDataError(f"{key} must be a non-empty string")
    return value.strip()


def _strings(payload: Mapping[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if type(value) is str:
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise PublicDataError(f"{key} is invalid JSON") from exc
    if type(value) is not list or any(
        type(item) is not str or not item.strip() for item in value
    ):
        raise PublicDataError(f"{key} must be a string array")
    return [item.strip() for item in value]


def _timestamp(value: object) -> datetime:
    if type(value) is not str or not value.strip():
        raise PublicDataError("timestamp must be an explicit string")
    raw = value.strip()
    try:
        if raw.isdigit() and len(raw) == 13:
            return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
        if "T" in raw:
            return require_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except (OverflowError, ValueError) as exc:
        raise PublicDataError("invalid timestamp") from exc
    raise PublicDataError("ambiguous timestamp unit or format")


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise PublicDataError(f"{name} is not numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PublicDataError(f"{name} is invalid") from exc
    if not result.is_finite() or result <= 0:
        raise PublicDataError(f"{name} must be positive")
    return result


def _parse_market(payload: Mapping[str, Any], *, asset: str = "BTC") -> MarketMetadata:
    pattern = _ASSET_PATTERNS.get(asset)
    if pattern is None:
        raise PublicDataError(f"unsupported asset: {asset}")
    title = _string(payload, "question")
    rules = _string(payload, "description")
    if not pattern.search(f"{title}\n{rules}") or not _UP_DOWN.search(
        f"{title}\n{rules}"
    ):
        raise PublicDataError(f"not an {asset} Up/Down market")
    outcomes = _strings(payload, "outcomes")
    tokens = _strings(payload, "clobTokenIds")
    if (
        tuple(item.casefold() for item in outcomes) != ("up", "down")
        or len(tokens) != 2
        or tokens[0] == tokens[1]
    ):
        raise PublicDataError("ambiguous outcomes or tokens")
    start = _timestamp(payload.get("eventStartTime", payload.get("startDate")))
    end = _timestamp(payload.get("endDate"))
    seconds = (end - start).total_seconds()
    intervals = tuple(
        interval
        for interval in MarketInterval
        if seconds == interval.seconds and start.timestamp() % interval.seconds == 0
    )
    if (
        len(intervals) != 1
        or payload.get("active") is not True
        or payload.get("closed") is True
    ):
        raise PublicDataError("market interval or state is invalid")
    return MarketMetadata(
        market_id=_string(payload, "conditionId"),
        title=title,
        rules=rules,
        asset=asset,
        interval=intervals[0],
        window_start=start,
        window_end=end,
        up_token_id=tokens[0],
        down_token_id=tokens[1],
        outcomes=("Up", "Down"),
    )


def _levels(value: object, name: str) -> tuple[BookLevel, ...]:
    if type(value) is not list:
        raise PublicDataError(f"{name} must be a list")
    return tuple(
        BookLevel(
            price=_decimal(_object(item, name).get("price"), "price"),
            size=_decimal(_object(item, name).get("size"), "size"),
        )
        for item in value
    )


def _book(
    http: PublicGetProtocol,
    market: MarketMetadata,
    outcome: OutcomeSide,
    clock: Callable[[], datetime],
) -> OrderBook:
    token = market.up_token_id if outcome is OutcomeSide.UP else market.down_token_id
    payload = _object(http.json(f"{CLOB}/book", params={"token_id": token}), "book")
    if (
        _string(payload, "asset_id") != token
        or _string(payload, "market") != market.market_id
    ):
        raise PublicDataError("book identity mismatch")
    bids = tuple(
        sorted(
            _levels(payload.get("bids"), "bids"),
            key=lambda level: level.price,
            reverse=True,
        )
    )
    asks = tuple(
        sorted(_levels(payload.get("asks"), "asks"), key=lambda level: level.price)
    )
    received_at = require_utc(clock())
    exchange_at = _timestamp(payload.get("timestamp"))
    if exchange_at - received_at > MAX_CLOCK_SKEW:
        raise PublicDataError("book timestamp exceeds allowed clock skew")
    return OrderBook(
        market_id=market.market_id,
        token_id=token,
        outcome=outcome,
        sequence=0,
        bids=bids,
        asks=asks,
        tick_size=_decimal(payload.get("tick_size"), "tick_size"),
        exchange_at=min(exchange_at, received_at),
        received_at=received_at,
        tradable=bool(bids and asks),
        reason=None if bids and asks else "book side is empty",
    )


def _require_fresh_book(book: OrderBook, observed_at: datetime) -> None:
    if book.exchange_at > book.received_at or book.received_at > observed_at:
        raise PublicDataError("book timestamps are inconsistent")
    if (
        observed_at - book.exchange_at > MAX_BOOK_AGE
        or observed_at - book.received_at > MAX_BOOK_AGE
    ):
        raise PublicDataError("book is stale")


def _require_fresh_books(up: OrderBook, down: OrderBook, observed_at: datetime) -> None:
    for book in (up, down):
        _require_fresh_book(book, observed_at)


def _fee(http: PublicGetProtocol, market: MarketMetadata) -> FeeMetadata:
    payload = _object(
        http.json(f"{CLOB}/clob-markets/{market.market_id}"), "CLOB market"
    )
    if payload.get("c") != market.market_id or payload.get("ao") is not True:
        raise PublicDataError("CLOB market identity or state changed")
    fee = _object(payload.get("fd"), "fee metadata")
    rate = Decimal(str(fee.get("r")))
    exponent = Decimal(str(fee.get("e")))
    if not rate.is_finite() or rate < 0 or not exponent.is_finite() or exponent < 0:
        raise PublicDataError("fee metadata is invalid")
    return FeeMetadata(rate, exponent, _decimal(payload.get("mos"), "minimum size"))
